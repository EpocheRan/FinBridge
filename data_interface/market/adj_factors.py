"""复权因子提供者。

行情表只存不复权价，写入时必须同时拿到**累计后复权因子**。优先级：

1. Tushare ``adj_factor``（2000 积分可用，日频、全市场一次可取一个交易日）；
2. AkShare 新浪 ``stock_zh_a_daily(adjust="hfq-factor")``（单只、日频，兜底）；
3. 都拿不到 → 返回 ``None``，调用方写 NULL，读取端会标记
   ``adjust_fallback="missing_adj_factor"``（绝不假装因子=1）。

因子口径：单调递增的后复权累计因子，除权日跳变，不随新分红回溯变化。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.security_code import normalize_code, ticker_type

logger = logging.getLogger("DataInterface.AdjFactor")

_STOP_ERRORS = (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)

_CACHE_TTL_SECONDS = 600.0


class AdjFactorProvider:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        tushare=None,
        akshare=None,
    ):
        self.config = config or get_config()
        self._tushare = tushare
        self._akshare = akshare
        self._tushare_disabled = False
        self._cache: dict[tuple[str, str, str], tuple[pd.Series | None, float]] = {}

    # ------------------------------------------------------------------ 依赖
    @property
    def tushare(self):
        if self._tushare is None:
            from data_interface.financial.sources.tushare_source import (
                get_shared_tushare_source,
            )

            self._tushare = get_shared_tushare_source(self.config)
        return self._tushare

    @property
    def akshare(self):
        if self._akshare is None:
            from .sources.akshare_source import AkshareMarketSource

            self._akshare = AkshareMarketSource(self.config)
        return self._akshare

    # ------------------------------------------------------------------ 单只
    def daily_factors(self, code: str, start_date: str, end_date: str) -> pd.Series | None:
        """返回 ``index=日期(YYYY-MM-DD), values=复权因子``；拿不到返回 None。

        带 10 分钟进程内缓存：一次心跳里多只股票/多次增量不会重复打 Tushare 配额。
        """
        code = normalize_code(code)
        if not code:
            return None
        key = (code, start_date, end_date)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]

        series = self._from_tushare(code, start_date, end_date)
        if series is None or series.empty:
            series = self._from_akshare(code, start_date, end_date)
        if series is None or series.empty:
            logger.warning("未获取到 %s 的复权因子（%s ~ %s）", code, start_date, end_date)
            self._cache[key] = (None, time.monotonic())
            return None
        self._cache[key] = (series, time.monotonic())
        return series

    def _from_tushare(self, code: str, start: str, end: str) -> pd.Series | None:
        if self._tushare_disabled:
            return None
        try:
            rows = self.tushare.fetch_adj_factor(
                code, start_date=start.replace("-", ""), end_date=end.replace("-", "")
            )
        except _STOP_ERRORS as exc:
            logger.warning("Tushare adj_factor 不可用，降级 AkShare: %s", exc)
            self._tushare_disabled = isinstance(
                exc, (DailyQuotaExceededError, PermissionDeniedError)
            )
            return None
        except Exception as exc:
            logger.warning("Tushare adj_factor 失败，降级 AkShare: %s", exc)
            return None
        return _rows_to_series(rows, value_key="adj_factor")

    def _from_akshare(self, code: str, start: str, end: str) -> pd.Series | None:
        if ticker_type(code) != "stock":
            # 新浪 hfq-factor 只覆盖股票；ETF/LOF 暂无稳定因子源
            return None
        try:
            frame = self.akshare.fetch_adj_factor(code, start, end)
        except Exception as exc:
            logger.warning("AkShare 复权因子失败 %s: %s", code, exc)
            return None
        if frame is None or frame.empty:
            return None
        return _rows_to_series(frame.to_dict("records"), value_key="adj_factor")

    # ------------------------------------------------------------------ 全市场
    def factors_for_trade_date(self, trade_date: str) -> dict[str, float]:
        """某个交易日全市场因子（Tushare 一次返回全市场）。"""
        if self._tushare_disabled:
            return {}
        try:
            rows = self.tushare.fetch_adj_factor_all(trade_date)
        except _STOP_ERRORS as exc:
            logger.warning("Tushare adj_factor 全市场不可用: %s", exc)
            return {}
        except Exception as exc:
            logger.warning("Tushare adj_factor 全市场失败: %s", exc)
            return {}
        result: dict[str, float] = {}
        for row in rows or []:
            code = normalize_code(row.get("ts_code"))
            value = row.get("adj_factor")
            if code and value not in (None, ""):
                try:
                    result[code] = float(value)
                except (TypeError, ValueError):
                    continue
        return result


def _rows_to_series(rows: Iterable[dict[str, Any]], *, value_key: str) -> pd.Series | None:
    index: list[str] = []
    values: list[float] = []
    for row in rows or []:
        date = row.get("trade_date") or row.get("date") or row.get("dividOperateDate")
        value = row.get(value_key)
        if date in (None, "") or value in (None, ""):
            continue
        try:
            values.append(float(value))
            index.append(pd.to_datetime(str(date)).strftime("%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    series = pd.Series(values, index=pd.Index(index), dtype="float64")
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def attach_adj_factor(frame: pd.DataFrame, factors: pd.Series | None) -> pd.DataFrame:
    """把日频因子贴到行情帧上（分钟线按日期映射）。

    已有 ``adj_factor`` 列且非空的行保留原值。
    """
    if frame is None or frame.empty:
        return frame
    result = frame.copy()
    if factors is None or factors.empty:
        if "adj_factor" not in result.columns:
            result["adj_factor"] = pd.NA
        return result

    if not isinstance(result.index, pd.DatetimeIndex):
        result.index = pd.to_datetime(result.index)

    day_keys = pd.Series(result.index.strftime("%Y-%m-%d"), index=result.index)
    # 非交易日（周末/节假日时间戳）沿用最近一个有因子的交易日
    mapped = day_keys.map(factors)
    # 一批里可能有多只股票：按 code 分组前/后填充，避免跨股票串因子
    if "code" in result.columns:
        mapped = mapped.groupby(result["code"].astype(str)).transform(
            lambda item: item.ffill().bfill()
        )
    else:
        mapped = mapped.ffill().bfill()

    if "adj_factor" in result.columns:
        existing = pd.to_numeric(result["adj_factor"], errors="coerce")
        result["adj_factor"] = existing.where(existing.notna(), mapped)
    else:
        result["adj_factor"] = mapped
    return result
