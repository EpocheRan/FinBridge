"""Tushare Pro 数据源。

适配 2120 积分权限：

- 可用：``income`` / ``balancesheet`` / ``cashflow`` / ``fina_indicator`` /
  ``daily_basic`` / ``stock_basic`` / ``dividend`` / ``trade_cal``（均 2000 分档）
- 受限：``report_rc`` 2000 分仅试用，每天 10 次请求；本模块用本地配额文件
  做硬限制，超过后抛出 ``DailyQuotaExceededError``。
- 不使用任何 ``*_vip`` 接口（5000 分档）。

服务端限流错误会被转换为带接口名、限额、建议等待时间的结构化异常。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from data_interface.common.config import get_config, masked_tushare_token, project_path
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.rate_limiter import SlidingWindowRateLimiter

from ..normalizer import to_ts_code

logger = logging.getLogger("DataInterface.Tushare")


class DailyQuotaGuard:
    """按自然日限制某个接口的请求次数。"""

    def __init__(self, state_file: str | Path, limit: int):
        self.path = Path(state_file)
        if not self.path.is_absolute():
            self.path = project_path(str(self.path))
        self.limit = limit

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def consume(self, interface: str) -> int:
        today = date.today().isoformat()
        data = self._load()
        bucket = data.setdefault(today, {})
        used = int(bucket.get(interface, 0))
        if used >= self.limit:
            raise DailyQuotaExceededError(
                f"接口 {interface} 今日请求次数已达上限 {self.limit} 次",
                interface=interface,
                limit=self.limit,
            )
        bucket[interface] = used + 1
        data[today] = bucket
        # 只保留最近 7 天，避免文件无限增长
        for key in sorted(data.keys())[:-7]:
            data.pop(key, None)
        self._save(data)
        return bucket[interface]


class TushareSource:
    name = "tushare"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or get_config()
        cfg = self.config["tushare"]
        self.enabled = bool(cfg.get("enabled", True))
        self.token = cfg.get("token")
        self.max_retries = int(cfg.get("max_retries", 3))
        self.backoff = float(cfg.get("retry_backoff_seconds", 2.0))
        self.limiter = SlidingWindowRateLimiter(
            int(cfg.get("requests_per_minute", 200)), 60.0
        )
        # 2120 积分下 report_rc 额外限制为 1 次/分钟，单独限流
        self.report_rc_limiter = SlidingWindowRateLimiter(1, 60.0)
        self.quota_guard = DailyQuotaGuard(
            cfg.get("quota_state_file", "data/runtime/tushare_quota.json"),
            int(cfg.get("report_rc_daily_limit", 10)),
        )
        self._pro = None
        if not self.enabled:
            raise SourceUnavailableError("Tushare 已在配置中禁用", source="tushare")
        if not self.token:
            raise SourceUnavailableError("config.json 缺少 tushare.token", source="tushare")

    @property
    def pro(self):
        if self._pro is None:
            try:
                import tushare as ts
            except ImportError as exc:
                raise SourceUnavailableError(
                    "未安装 tushare，请执行 uv sync", source="tushare"
                ) from exc
            try:
                self._pro = ts.pro_api(self.token)
            except Exception as exc:
                raise SourceUnavailableError(
                    f"Tushare 初始化失败: {exc}", source="tushare"
                ) from exc
            logger.info(
                "Tushare initialized (token=%s, points=%s)",
                masked_tushare_token(self.token),
                self.config["tushare"].get("points"),
            )
        return self._pro

    # ------------------------------------------------------------------ 限流
    def _classify_error(self, interface: str, exc: Exception) -> None:
        message = str(exc)
        lower = message.lower()

        if "每天最多" in message or "daily" in lower:
            limit = (
                int(self.config["tushare"].get("report_rc_daily_limit", 10))
                if interface == "report_rc"
                else int(
                    self.config["tushare"].get("daily_request_limit_per_api", 100000)
                )
            )
            raise DailyQuotaExceededError(
                f"Tushare 接口 {interface} 今日总量已达上限: {message}",
                interface=interface,
                limit=limit,
            ) from exc

        if (
            "每分钟" in message
            or "频次" in message
            or "per minute" in lower
        ):
            match = re.search(r"(\d+)\s*次\s*/\s*分钟", message)
            if not match:
                match = re.search(r"(\d+)\s*次", message)
            limit = int(match.group(1)) if match else int(
                self.config["tushare"].get("requests_per_minute", 200)
            )
            raise RateLimitExceededError(
                f"Tushare 接口 {interface} 触发分钟级限流: {message}",
                interface=interface,
                limit=limit,
                retry_after_seconds=60,
            ) from exc

        if "权限" in message or "积分" in message or "token" in lower:
            raise PermissionDeniedError(
                f"Tushare 接口 {interface} 权限不足: {message}",
                interface=interface,
                required_points=int(self.config["tushare"].get("points", 2120)),
            ) from exc

        raise SourceUnavailableError(
            f"Tushare 接口 {interface} 调用失败: {message}",
            source="tushare",
            detail=message,
        ) from exc

    def _call(self, interface: str, func: Callable, **kwargs) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.limiter.acquire()
            try:
                df = func(**kwargs)
                if df is None or getattr(df, "empty", False):
                    return []
                return df.to_dict("records")
            except (DailyQuotaExceededError, PermissionDeniedError):
                raise
            except RateLimitExceededError as exc:
                last_error = exc
                wait = exc.retry_after_seconds or 60
                logger.warning(
                    "Tushare %s 触发限流，第 %s/%s 次重试，等待 %s 秒",
                    interface,
                    attempt,
                    self.max_retries,
                    wait,
                )
                if attempt < self.max_retries:
                    time.sleep(wait)
            except Exception as exc:
                message = str(exc)
                if "每分钟最多" in message:
                    last_error = exc
                    if attempt < self.max_retries:
                        time.sleep(60)
                        continue
                    self._classify_error(interface, exc)
                if (
                    "每天最多" in message
                    or "权限" in message
                    or "积分" in message
                    or "频次" in message
                ):
                    self._classify_error(interface, exc)
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff * attempt)
                    continue
                self._classify_error(interface, exc)

        if isinstance(last_error, RateLimitExceededError):
            raise last_error
        raise SourceUnavailableError(
            f"Tushare 接口 {interface} 重试耗尽: {last_error}", source="tushare"
        )

    # ------------------------------------------------------------------ 报表
    def fetch_income(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
        report_type: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs = {"ts_code": to_ts_code(code)}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        if period:
            kwargs["period"] = period
        if report_type:
            # Tushare: 1=合并报表 2=单季合并 3=调整合并 ...
            kwargs["report_type"] = report_type
        return self._call("income", self.pro.income, **kwargs)

    def fetch_balance_sheet(self, code: str, **kwargs) -> list[dict[str, Any]]:
        kwargs.setdefault("ts_code", to_ts_code(code))
        return self._call("balancesheet", self.pro.balancesheet, **kwargs)

    def fetch_cash_flow(self, code: str, **kwargs) -> list[dict[str, Any]]:
        kwargs.setdefault("ts_code", to_ts_code(code))
        return self._call("cashflow", self.pro.cashflow, **kwargs)

    # ------------------------------------------------------------------ 复权因子
    def fetch_adj_factor(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        trade_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """单只股票的复权因子（累计后复权）。"""
        kwargs: dict[str, Any] = {"ts_code": to_ts_code(code)}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        if trade_date:
            kwargs["trade_date"] = trade_date
        return self._call("adj_factor", self.pro.adj_factor, **kwargs)

    def fetch_adj_factor_all(self, trade_date: str) -> list[dict[str, Any]]:
        """某个交易日全市场复权因子（一次返回全市场）。"""
        return self._call("adj_factor", self.pro.adj_factor, trade_date=trade_date)

    def fetch_fina_indicator(self, code: str, **kwargs) -> list[dict[str, Any]]:
        kwargs.setdefault("ts_code", to_ts_code(code))
        start_text = kwargs.get("start_date")
        end_text = kwargs.get("end_date")
        # Tushare fina_indicator 单次最多 100 条；按 5 年分段避免长历史被截断
        if start_text and end_text and len(start_text) == 8 and len(end_text) == 8:
            start_year = int(start_text[:4])
            end_year = int(end_text[:4])
            if end_year - start_year > 5:
                collected: list[dict[str, Any]] = []
                for chunk_start in range(start_year, end_year + 1, 5):
                    chunk_end = min(chunk_start + 4, end_year)
                    chunk_kwargs = dict(kwargs)
                    chunk_kwargs["start_date"] = f"{chunk_start}0101"
                    chunk_kwargs["end_date"] = f"{chunk_end}1231"
                    collected.extend(
                        self._call(
                            "fina_indicator", self.pro.fina_indicator, **chunk_kwargs
                        )
                    )
                # 按报告期去重
                deduped = {}
                for item in collected:
                    deduped[str(item.get("end_date"))] = item
                return list(deduped.values())
        return self._call("fina_indicator", self.pro.fina_indicator, **kwargs)

    # ------------------------------------------------------------------ 行情/估值
    def fetch_daily_basic(
        self,
        code: str | None = None,
        *,
        trade_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if code:
            kwargs["ts_code"] = to_ts_code(code)
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return self._call("daily_basic", self.pro.daily_basic, **kwargs)

    def fetch_stock_basic(self, code: str | None = None, **kwargs) -> list[dict[str, Any]]:
        # 不显式传 fields 时，Tushare 默认不返回 list_status / delist_date，
        # 会导致 profile 写入 list_status=NULL，进而从 A 股股票池消失。
        kwargs.setdefault(
            "fields",
            "ts_code,symbol,name,area,industry,fullname,enname,market,"
            "exchange,list_status,list_date,delist_date,is_hs",
        )
        if code:
            kwargs["ts_code"] = to_ts_code(code)
        return self._call("stock_basic", self.pro.stock_basic, **kwargs)

    def fetch_price_history(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        freq: str = "d",
    ) -> list[dict[str, Any]]:
        """按代码拉取股票日/周/月线（不复权）。

        Tushare 的 ``stk_mins`` 需要更高积分，因此分钟线不在本方法内支持。
        """
        interface = {"d": "daily", "w": "weekly", "m": "monthly"}.get(freq)
        if interface is None:
            raise ValueError(f"Tushare 行情不支持频率: {freq}")
        kwargs: dict[str, Any] = {"ts_code": to_ts_code(code)}
        if start_date:
            kwargs["start_date"] = str(start_date).replace("-", "")
        if end_date:
            kwargs["end_date"] = str(end_date).replace("-", "")
        return self._call(f"price_{freq}", getattr(self.pro, interface), **kwargs)

    def fetch_index_daily(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """指数日线（如 000300 沪深300），用于 Beta 基准。

        Tushare 的指数后缀与股票不同：沪市指数 ``.SH``（000300.SH），
        深市指数 ``.SZ``（399006.SZ），按代码段推断。
        """
        normalized = str(code).split(".")[0]
        suffix = "SZ" if normalized.startswith(("399", "899")) else "SH"
        kwargs: dict[str, Any] = {"ts_code": f"{normalized}.{suffix}"}
        if start_date:
            kwargs["start_date"] = str(start_date).replace("-", "")
        if end_date:
            kwargs["end_date"] = str(end_date).replace("-", "")
        return self._call("index_daily", self.pro.index_daily, **kwargs)

    def fetch_daily_all(self, trade_date: str) -> list[dict[str, Any]]:
        """按交易日批量拉全市场日线（Tushare daily，一次返回全市场）。"""
        return self._call("daily", self.pro.daily, trade_date=trade_date)

    def fetch_daily_basic_all(self, trade_date: str) -> list[dict[str, Any]]:
        """按交易日批量拉全市场每日指标（用于换手率）。"""
        return self._call("daily_basic", self.pro.daily_basic, trade_date=trade_date)

    def fetch_fund_basic_all(self, market: str = "E") -> list[dict[str, Any]]:
        """拉取场内基金列表（ETF/LOF）。"""
        return self._call("fund_basic", self.pro.fund_basic, market=market)

    def fetch_fund_daily_all(self, trade_date: str) -> list[dict[str, Any]]:
        """按交易日批量拉全市场 ETF/LOF 日线。"""
        return self._call("fund_daily", self.pro.fund_daily, trade_date=trade_date)

    def fetch_dividend(self, code: str, **kwargs) -> list[dict[str, Any]]:
        kwargs.setdefault("ts_code", to_ts_code(code))
        return self._call("dividend", self.pro.dividend, **kwargs)

    def fetch_moneyflow(self, **kwargs) -> list[dict[str, Any]]:
        """个股资金流（四档）。支持 ``trade_date`` 或 ``ts_code+start_date+end_date``。"""
        return self._call("moneyflow", self.pro.moneyflow, **kwargs)

    def fetch_concept(self) -> list[dict[str, Any]]:
        """概念分类树（Tushare ``concept``）。"""
        return self._call("concept", self.pro.concept)

    def fetch_trade_cal(
        self, start_date: str, end_date: str, exchange: str = "SSE"
    ) -> list[dict[str, Any]]:
        return self._call(
            "trade_cal",
            self.pro.trade_cal,
            exchange=exchange,
            start_date=start_date,
            end_date=end_date,
        )

    # ------------------------------------------------------------------ 一致预期
    def fetch_report_rc(
        self,
        code: str,
        *,
        report_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """券商一致预期。

        2120 积分下 ``report_rc`` 每天只有 10 次试用额度，因此这里先做本地
        配额检查；默认建议按需调用，不要在全市场循环里使用。
        """
        if not self.report_rc_limiter.try_acquire():
            raise RateLimitExceededError(
                "Tushare report_rc 在 2120 积分下限制为 1 次/分钟，请稍后再试",
                interface="report_rc",
                limit=1,
                retry_after_seconds=60,
            )
        self.quota_guard.consume("report_rc")
        kwargs: dict[str, Any] = {"ts_code": to_ts_code(code)}
        if report_date:
            kwargs["report_date"] = report_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return self._call("report_rc", self.pro.report_rc, **kwargs)

    def health_check(self) -> dict[str, Any]:
        try:
            rows = self.fetch_stock_basic(code="600519")
            return {
                "ok": True,
                "source": "tushare",
                "token": masked_tushare_token(self.token),
                "sample_rows": len(rows),
            }
        except Exception as exc:
            return {"ok": False, "source": "tushare", "error": str(exc)}

# ---------------------------------------------------------------------------
# 进程内共享实例
# ---------------------------------------------------------------------------
_shared_sources: dict[str, TushareSource] = {}
_shared_lock = threading.Lock()


def get_shared_tushare_source(config: dict[str, Any] | None = None) -> TushareSource:
    """按 token 复用同一个 ``TushareSource``（同一个限流器与配额账本）。

    行情旁路 (``TushareMarketSource``)、财务服务、定时任务、复权因子各自 new
    一个 ``TushareSource`` 时会各持一套滑动窗口限流与每日配额，实际配额能被
    放大数倍；这里统一收口。
    """
    from data_interface.common.config import get_config

    cfg = config or get_config()
    key = str(cfg["tushare"].get("token") or "")
    with _shared_lock:
        source = _shared_sources.get(key)
        if source is None:
            source = TushareSource(cfg)
            _shared_sources[key] = source
        return source
