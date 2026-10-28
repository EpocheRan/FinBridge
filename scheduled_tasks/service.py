"""A 股全量日线/财务数据同步任务。

模块本身不负责定时，只提供可被 Agent 心跳反复调用的「有界批次」：

- 日线：按交易日批量拉取全市场（一次 Tushare daily 返回全市场），
  可选同时拉 ETF/LOF（``include_funds``）；
- 基准指数：补齐 Beta 所需的基准指数日线；
- 财务：按股票池分批，逐只拉取三大报表、派生指标与估值；
- Screener：按日期范围构建/修复日频因子快照；
- 每次只处理有限数量，返回 next_cursor / next_cursors，下一次心跳从游标继续。

2026-10 修订：

- 日线写入 **不复权价 + adj_factor**（按交易日一次取全市场因子）；
- 某交易日拿不到数据时不再直接跳过：记 ``pending`` 并在后续心跳重试，
  超过上限才放弃并记 error，避免永久缺口；
- 支持 ``include_funds``（ETF/LOF 日线 + 基金基础信息）；
- 支持 ``codes`` 指定股票/ETF 代码（不传则全市场），
  指定代码时任务状态使用独立的 ``daily_codes`` / ``financial_codes`` 游标，
  不会覆盖全市场回填进度；
- ``run(dataset="all")`` 可一次编排日线、基准、财务、Screener 四个阶段。
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import (
    is_a_share_code,
    normalize_code,
    ticker_type,
)
from data_interface.common.timeutil import market_closed_today, now_cn, today_str
from data_interface.common.trading_calendar import TradingCalendar
from data_interface.concept.repository import ConceptRepository
from data_interface.concept.service import ConceptService
from data_interface.concept.sources.em_source import EmConceptSource
from data_interface.concept.sources.ths_scrape_source import ThsScrapeConceptSource
from data_interface.concept.sources.ths_source import ThsConceptSource
from data_interface.concept.sources.tushare_classification import (
    TushareConceptClassificationSource,
)
from data_interface.financial.service import FinancialDataService
from data_interface.financial.sources.tushare_source import (
    TushareSource,
    get_shared_tushare_source,
)
from data_interface.market.adj_factors import AdjFactorProvider
from data_interface.market.fetcher import MarketDataFetcher
from data_interface.moneyflow.repository import MoneyflowRepository
from data_interface.moneyflow.service import MoneyflowService
from data_interface.moneyflow.sources.tushare_source import TushareMoneyflowSource

from .state import TaskStateRepository

logger = logging.getLogger("ScheduledTasks.AShareSync")

TASK_NAME = "a_share_sync"
STOP_ERRORS = (
    RateLimitExceededError,
    DailyQuotaExceededError,
    PermissionDeniedError,
    SourceUnavailableError,
)

# 全市场与指定代码使用不同状态键，避免互相覆盖游标。
DAILY_STATE = "daily"
DAILY_CODES_STATE = "daily_codes"
FINANCIAL_STATE = "financial"
FINANCIAL_CODES_STATE = "financial_codes"
BENCHMARK_STATE = "benchmark"
SCREENER_STATE = "screener"
MONEYFLOW_STATE = "moneyflow"
CONCEPT_META_STATE = "concept_meta"
CONCEPT_DAILY_STATE = "concept_daily"
CONCEPT_MEMBER_STATE = "concept_member"
CONCEPT_CLASS_STATE = "concept_class"

DATASETS = (
    "daily",
    "benchmark",
    "financial",
    "screener",
    "moneyflow",
    "concept_meta",
    "concept_daily",
    "concept_member",
    "concept_class",
    "all",
)


def normalize_selected_codes(
    codes: Sequence[str] | str | None,
) -> list[str] | None:
    """把用户传入的代码统一成排序后的 6 位代码列表。

    ``None``、空列表、空字符串都表示「不指定 = 全市场」。
    """
    if codes is None:
        return None
    if isinstance(codes, str):
        items = codes.replace(";", ",").split(",")
    else:
        items = [str(item) for item in codes]

    normalized: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        code = normalize_code(text)
        if len(code) != 6:
            logger.warning("忽略无法识别的证券代码: %s", text)
            continue
        normalized.add(code)
    return sorted(normalized) or None


def parse_date_range(
    start_date: str | None,
    end_date: str | None,
    *,
    default_start: str | None = None,
) -> tuple[date, date]:
    """解析并校验 YYYY-MM-DD 日期范围。"""
    end = date.fromisoformat(str(end_date)[:10]) if end_date else date.fromisoformat(today_str())
    if start_date:
        start = date.fromisoformat(str(start_date)[:10])
    elif default_start:
        start = date.fromisoformat(str(default_start)[:10])
    else:
        start = end
    if start > end:
        raise ValueError(f"start_date({start}) 不能晚于 end_date({end})")
    return start, end


class AShareSyncService:
    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        db: QuestDBClient | None = None,
        tushare: TushareSource | None = None,
        financial: FinancialDataService | None = None,
        state: TaskStateRepository | None = None,
        market_fetcher: MarketDataFetcher | None = None,
        trading_calendar: TradingCalendar | None = None,
        adj_factors: AdjFactorProvider | None = None,
        screener_app_factory: Callable[[], Any] | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("scheduled_tasks", {})
        self.db = db or getattr(financial, "db", None) or QuestDBClient()
        self.tushare = tushare or getattr(financial, "tushare", None) or get_shared_tushare_source(self.config)
        self.financial = financial or FinancialDataService(
            db=self.db, tushare=self.tushare, config=self.config
        )
        self.market_fetcher = market_fetcher or MarketDataFetcher(
            "questdb", config=self.config, db=self.db
        )
        self.trading_calendar = trading_calendar or TradingCalendar(self.db)
        self.state = state or TaskStateRepository(self.db)
        self.adj_factors = adj_factors or AdjFactorProvider(
            self.config, tushare=self.tushare
        )
        self._screener_app_factory = screener_app_factory
        self._screener_app_instance: Any | None = None
        self._moneyflow_service: MoneyflowService | None = None
        self._concept_service: ConceptService | None = None

    def _get_screener_app(self) -> Any:
        """惰性构建 screener 应用，避免纯日线/财务任务加载整个 screener。"""
        if self._screener_app_instance is None:
            if self._screener_app_factory is not None:
                self._screener_app_instance = self._screener_app_factory()
            else:
                from screener.bootstrap import build_screener_application

                self._screener_app_instance = build_screener_application(self.config)
        return self._screener_app_instance

    def _get_moneyflow_service(self) -> MoneyflowService:
        if self._moneyflow_service is None:
            self._moneyflow_service = MoneyflowService(
                config=self.config,
                repository=MoneyflowRepository(db=self.db, config=self.config),
                sources=[
                    TushareMoneyflowSource(self.config, tushare=self.tushare)
                ],
            )
        return self._moneyflow_service

    def _get_concept_service(self) -> ConceptService:
        if self._concept_service is None:
            self._concept_service = ConceptService(
                config=self.config,
                repository=ConceptRepository(db=self.db, config=self.config),
                board_sources=[ThsConceptSource(), EmConceptSource()],
                daily_source=ThsConceptSource(),
                member_sources=[EmConceptSource(), ThsScrapeConceptSource()],
                classification_source=TushareConceptClassificationSource(
                    self.config, tushare=self.tushare
                ),
            )
        return self._concept_service

    # ------------------------------------------------------------------ 日线
    def sync_daily(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_funds: bool | None = None,
        codes: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        selected_codes = normalize_selected_codes(codes)
        selected_set = set(selected_codes or ())
        state_dataset = DAILY_STATE if selected_codes is None else DAILY_CODES_STATE

        if include_funds is None:
            if selected_codes is None:
                include_funds = bool(self.settings.get("include_funds", True))
            else:
                # 指定代码时只为基金代码额外拉 ETF/LOF，避免无意义的全市场基金查询
                include_funds = any(
                    ticker_type(code) in ("etf", "lof", "fund")
                    for code in selected_codes
                )
        else:
            include_funds = bool(include_funds)

        if start_date is None:
            if selected_codes is None:
                start_date = self.settings.get("daily_start_date", "2020-01-01")
            else:
                # 指定代码的首次更新默认只看最近约 400 天，避免误触发全历史回填；
                # 需要完整历史时显式传 start_date。
                start_date = (
                    date.fromisoformat(today_str()) - timedelta(days=400)
                ).isoformat()
        start_date = str(start_date)[:10]
        end_date = str(end_date or today_str())[:10]
        if start_date > end_date:
            raise ValueError(f"start_date({start_date}) 不能晚于 end_date({end_date})")
        max_trade_days = max(1, int(max_trade_days or self.settings.get("daily_batch_days", 20)))
        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        payload: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "include_funds": include_funds,
            "codes": selected_codes,
        }

        previous = None if reset else self.state.get(TASK_NAME, state_dataset)
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}
        # 起始日期、基金口径或代码清单变化视为新任务
        if old_payload and (
            old_payload.get("start_date") != start_date
            or bool(old_payload.get("include_funds")) != include_funds
            or old_payload.get("codes") != selected_codes
        ):
            previous = None
            old_payload = {}

        attempts: dict[str, int] = dict(old_payload.get("attempts") or {})
        pending: list[str] = list(old_payload.get("pending") or [])
        payload["attempts"] = attempts
        payload["pending"] = pending

        # 日历不可用/未覆盖时必须回退工作日；直接 get_trading_days 会把
        # “日历拉取失败”伪装成“区间内没有交易日”，任务静默 0 行。
        all_days = self.trading_calendar.trading_days_or_weekdays(start_date, end_date)
        if not all_days:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=0,
                processed=0,
                payload=payload,
            )
            return {
                "dataset": "daily",
                "codes": selected_codes,
                "done": True,
                "total": 0,
                "processed": 0,
                "next_cursor": None,
                "rows_written": 0,
                "fund_rows_written": 0,
                "errors": [],
            }

        current_cursor = cursor or (previous or {}).get("cursor")
        # 有 pending（上次没拿到的交易日）时，从最近的那个 pending 重试
        if not cursor and pending:
            earliest_pending = min(pending)
            current_cursor = min(current_cursor, earliest_pending) if current_cursor else earliest_pending

        days = [day for day in all_days if not current_cursor or day >= current_cursor]
        if not days:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=current_cursor,
                status="DONE",
                total=len(all_days),
                processed=len(all_days),
                payload=payload,
            )
            return {
                "dataset": "daily",
                "codes": selected_codes,
                "done": True,
                "total": len(all_days),
                "processed": len(all_days),
                "next_cursor": None,
                "rows_written": 0,
                "fund_rows_written": 0,
                "errors": [],
            }

        batch = days[:max_trade_days]
        rows_written = 0
        fund_rows_written = 0
        errors: list[str] = []
        paused_at: str | None = None

        # 指定代码时不重建全市场基金池，避免一次少量更新触发全量基金同步。
        if include_funds and selected_codes is None:
            try:
                self._ensure_fund_universe()
            except STOP_ERRORS:
                raise
            except Exception as exc:
                logger.warning("基金基础信息同步失败（继续拉日线）: %s", exc)

        for day in batch:
            trade_date = day.replace("-", "")
            try:
                raw = self.tushare.fetch_daily_all(trade_date)
                basic = self.tushare.fetch_daily_basic_all(trade_date) if raw else []
                if not raw:
                    should_pause = self._handle_empty_day(
                        day, attempts, pending, errors, retry_limit
                    )
                    if should_pause:
                        paused_at = day
                        break
                    continue

                frame = self._daily_frame(raw, basic)
                if selected_codes is not None and not frame.empty:
                    frame = frame[frame["code"].isin(selected_set)]
                frame = self._attach_factors(frame, trade_date)
                if not frame.empty:
                    rows_written += self.market_fetcher.update_ohlc(frame, "d")

                if include_funds:
                    fund_frame = self._fund_daily_frame(
                        self.tushare.fetch_fund_daily_all(trade_date)
                    )
                    if selected_codes is not None and not fund_frame.empty:
                        fund_frame = fund_frame[fund_frame["code"].isin(selected_set)]
                    if not fund_frame.empty:
                        fund_frame = self._attach_factors(fund_frame, trade_date)
                        # 股票/基金分开写：混在一起会让 rows_written 把基金也算成股票，
                        # 且 pandas 对「含全 NA 列的 concat」会告警
                        fund_rows_written += self.market_fetcher.update_ohlc(
                            fund_frame, "d"
                        )
                attempts.pop(day, None)
                if day in pending:
                    pending.remove(day)
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=day,
                    status="PAUSED",
                    total=len(all_days),
                    processed=sum(1 for item in all_days if item < day),
                    message="rate limit or permission",
                    payload=payload,
                )
                raise
            except Exception as exc:
                logger.warning("日线批量同步失败 %s: %s", day, exc)
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=day,
                    status="PAUSED",
                    total=len(all_days),
                    processed=sum(1 for item in all_days if item < day),
                    message=str(exc)[:500],
                    payload=payload,
                )
                raise

        if paused_at:
            state_cursor = paused_at
            next_cursor = paused_at
            done = False
            processed = sum(1 for day in all_days if day < paused_at)
        else:
            more = len(days) > len(batch)
            last_day = batch[-1]
            state_cursor = days[len(batch)] if more else (
                datetime.strptime(last_day, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            next_cursor = days[len(batch)] if more else None
            done = not more and not pending
            processed = sum(1 for day in all_days if day < state_cursor)

        self.state.save(
            TASK_NAME,
            state_dataset,
            cursor=state_cursor,
            status="DONE" if done else ("PAUSED" if paused_at else "RUNNING"),
            total=len(all_days),
            processed=processed,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "daily",
            "codes": selected_codes,
            "include_funds": include_funds,
            "done": done,
            "total": len(all_days),
            "processed": processed,
            "next_cursor": next_cursor,
            "rows_written": rows_written,
            "fund_rows_written": fund_rows_written,
            "pending_days": sorted(pending)[:10],
            "errors": errors[:10],
        }

    def _handle_empty_day(
        self,
        day: str,
        attempts: dict[str, int],
        pending: list[str],
        errors: list[str],
        retry_limit: int,
    ) -> bool:
        """某交易日无数据：返回 True 表示应暂停在该日等待下次重试。"""
        attempts[day] = int(attempts.get(day, 0)) + 1
        if day not in pending:
            pending.append(day)
        if day >= today_str() and not market_closed_today():
            # 当天尚未收盘，Tushare 日线还没发布：不算错误，明天再补
            logger.info("%s 尚未发布日线（未收盘），留待下次心跳", day)
            return False
        if attempts[day] >= retry_limit:
            errors.append(f"{day}: 连续 {attempts[day]} 次无数据，跳过")
            logger.warning("%s 连续 %s 次无数据，跳过", day, attempts[day])
            if day in pending:
                pending.remove(day)
            return False
        logger.warning("%s 无数据（第 %s 次），暂停后重试", day, attempts[day])
        return True

    def _attach_factors(self, frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """贴上该交易日的全市场复权因子（拿不到就写 NULL，读取端会标记降级）。"""
        if frame.empty:
            return frame
        try:
            factors = self.adj_factors.factors_for_trade_date(trade_date)
        except Exception as exc:
            logger.warning("复权因子获取失败 %s: %s", trade_date, exc)
            factors = {}
        if factors:
            frame["adj_factor"] = frame["code"].map(factors)
        else:
            frame["adj_factor"] = pd.NA
        return frame

    @staticmethod
    def _daily_frame(
        raw_rows: list[dict[str, Any]],
        basic_rows: list[dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        frame = pd.DataFrame(raw_rows)
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        for source in ("open", "high", "low", "close"):
            frame[source] = pd.to_numeric(frame.get(source), errors="coerce")
        # Tushare daily: vol 单位「手」-> 股，amount 单位「千元」-> 元
        frame["volume"] = pd.to_numeric(frame.get("vol"), errors="coerce").fillna(0) * 100
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0) * 1000

        # 换手率来自 daily_basic.turnover_rate（%），避免把已有换手率覆盖成 0
        turnover = {}
        for item in basic_rows or []:
            code = normalize_code(str(item.get("ts_code") or ""))
            value = item.get("turnover_rate")
            if code and value is not None:
                try:
                    turnover[code] = float(value)
                except (TypeError, ValueError):
                    continue
        frame["turnover"] = frame["code"].map(turnover).astype("float64")
        frame = frame.dropna(subset=["ts", "code", "open", "high", "low", "close"])
        frame.set_index("ts", inplace=True)
        return frame[["code", "open", "high", "low", "close", "volume", "amount", "turnover"]]

    @staticmethod
    def _fund_daily_frame(raw_rows: list[dict[str, Any]]) -> pd.DataFrame:
        frame = pd.DataFrame(raw_rows or [])
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        for source in ("open", "high", "low", "close"):
            frame[source] = pd.to_numeric(frame.get(source), errors="coerce")
        # Tushare fund_daily: vol 单位「手」-> 股，amount 单位「千元」-> 元
        frame["volume"] = pd.to_numeric(frame.get("vol"), errors="coerce").fillna(0) * 100
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0) * 1000
        frame["turnover"] = pd.NA
        frame = frame.dropna(subset=["ts", "code", "open", "high", "low", "close"])
        frame.set_index("ts", inplace=True)
        return frame[["code", "open", "high", "low", "close", "volume", "amount", "turnover"]]

    def _ensure_fund_universe(self) -> int:
        """确保场内基金基础信息已落库（ETF/LOF 查询与同步都依赖它）。"""
        min_count = int(self.settings.get("fund_universe_min_count", 500))
        existing = self.financial.repository.get_fund_codes()
        if len(existing) >= min_count:
            return len(existing)

        raw = self.tushare.fetch_fund_basic_all("E")
        if not raw:
            return len(existing)

        snapshot = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        for item in raw:
            code = normalize_code(str(item.get("ts_code") or ""))
            if not code or self.market_fetcher.get_ticker_type(code) not in ("etf", "lof"):
                continue
            rows.append(
                {
                    "ts": snapshot,
                    "code": code,
                    "name": item.get("name"),
                    "fullname": item.get("name"),
                    "found_date": pd.to_datetime(item.get("found_date"), errors="coerce"),
                    "total_asset": None,
                    "management": item.get("management"),
                    "fund_type": item.get("fund_type"),
                    "rating_agency": None,
                    "rating": None,
                }
            )
        if not rows:
            return len(existing)
        frame = pd.DataFrame(rows)
        written = self.market_fetcher.update_fund_basic(frame)
        logger.info("基金基础信息同步 %s 条", written)
        return len(existing) + written

    # ------------------------------------------------------------------ 财务
    def sync_financials(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_stocks: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        codes: Sequence[str] | str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        selected_codes = normalize_selected_codes(codes)
        state_dataset = (
            FINANCIAL_STATE if selected_codes is None else FINANCIAL_CODES_STATE
        )

        if start_date:
            start_day = date.fromisoformat(str(start_date)[:10])
            start_year = start_day.year
        else:
            if selected_codes is None:
                start_day = date(
                    int(self.settings.get("financial_start_year", 2020)), 1, 1
                )
            else:
                # 指定代码的首次财务更新默认最近 2 年，避免无意回补全历史。
                start_day = date(
                    max(1990, date.fromisoformat(today_str()).year - 1), 1, 1
                )
            start_year = start_day.year
        if end_date:
            end_day = date.fromisoformat(str(end_date)[:10])
            end_year = end_day.year
        else:
            today = date.fromisoformat(today_str())
            end_day = date(today.year, 12, 31)
            end_year = today.year
        if start_day > end_day:
            raise ValueError(f"start_date({start_day}) 不能晚于 end_date({end_day})")

        valuation_start = start_day.isoformat() if start_date else f"{start_year}-01-01"
        valuation_end = end_day.isoformat() if end_date else f"{end_year}-12-31"
        if max_stocks is None:
            max_stocks = (
                self.settings.get("financial_batch_stocks", 15)
                if selected_codes is None
                else self.settings.get("financial_selected_batch_stocks", 50)
            )
        max_stocks = max(1, int(max_stocks))
        include_valuation = (
            bool(self.settings.get("include_valuation", False))
            if include_valuation is None
            else include_valuation
        )
        payload = {
            "start_year": start_year,
            "end_year": end_year,
            "valuation_start": valuation_start,
            "valuation_end": valuation_end,
            "include_valuation": include_valuation,
            "codes": selected_codes,
        }

        previous = None if reset or force else self.state.get(TASK_NAME, state_dataset)
        if previous and previous.get("payload"):
            try:
                old = json.loads(previous["payload"])
            except Exception:
                old = {}
            # 兼容旧状态（没有 valuation_start/end/codes 字段）：
            # 旧任务按年份回填时，默认估值区间就是整年。
            old_valuation_start = old.get("valuation_start") or (
                f"{old.get('start_year')}-01-01"
                if old.get("start_year")
                else None
            )
            old_valuation_end = old.get("valuation_end") or (
                f"{old.get('end_year')}-12-31" if old.get("end_year") else None
            )
            if (
                old.get("start_year") != start_year
                or old.get("end_year") != end_year
                or old_valuation_start != valuation_start
                or old_valuation_end != valuation_end
                or bool(old.get("include_valuation")) != include_valuation
                or old.get("codes") != selected_codes
            ):
                previous = None

        if previous and previous.get("status") == "DONE" and not reset and not cursor:
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": previous.get("total") or 0,
                "processed": previous.get("processed") or 0,
                "next_cursor": None,
                "stocks_written": 0,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        skipped_codes: list[str] = []
        if selected_codes is None:
            universe = self.financial.ensure_stock_universe(
                int(self.settings.get("min_universe_size", 1000))
            )
            universe = sorted(code for code in universe if is_a_share_code(code))
        else:
            universe = []
            for code in selected_codes:
                if is_a_share_code(code):
                    universe.append(code)
                else:
                    skipped_codes.append(code)
            universe = sorted(set(universe))
        if not universe:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=0,
                processed=0,
                message="没有可同步的 A 股代码",
                payload=payload,
            )
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": 0,
                "processed": 0,
                "next_cursor": None,
                "stocks_written": 0,
                "skipped_codes": skipped_codes,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        current_cursor = cursor or (previous or {}).get("cursor")
        start_index = bisect_left(universe, current_cursor) if current_cursor else 0
        batch = universe[start_index : start_index + max_stocks]
        if not batch:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=len(universe),
                processed=len(universe),
                payload=payload,
            )
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": len(universe),
                "processed": len(universe),
                "next_cursor": None,
                "stocks_written": 0,
                "skipped_codes": skipped_codes,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        errors: list[str] = []
        stocks_written = 0
        for offset, code in enumerate(batch, start=start_index):
            try:
                self.financial.sync_statements(
                    code, start_year=start_year, end_year=end_year, force=force
                )
                self.financial.sync_indicators(code)
                if include_valuation:
                    self.financial.sync_valuation(
                        code,
                        start_date=valuation_start,
                        end_date=valuation_end,
                        force=force,
                    )
                stocks_written += 1
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=code,
                    status="PAUSED",
                    total=len(universe),
                    processed=offset,
                    message="rate limit or permission",
                    payload=payload,
                )
                raise
            except Exception as exc:
                errors.append(f"{code}: {exc}")
                logger.warning("财务同步失败 %s: %s", code, exc)

        next_index = start_index + len(batch)
        next_cursor = universe[next_index] if next_index < len(universe) else None
        done = next_cursor is None
        self.state.save(
            TASK_NAME,
            state_dataset,
            cursor=next_cursor,
            status="DONE" if done else "RUNNING",
            total=len(universe),
            processed=next_index,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "financial",
            "codes": selected_codes,
            "done": done,
            "total": len(universe),
            "processed": next_index,
            "next_cursor": next_cursor,
            "stocks_written": stocks_written,
            "skipped_codes": skipped_codes,
            "valuation_range": [valuation_start, valuation_end],
            "errors": errors[:10],
        }

    # ------------------------------------------------------------------ 基准
    def sync_benchmark(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        app: Any | None = None,
    ) -> dict[str, Any]:
        """补齐基准指数日线（默认沪深 300，Beta 依赖）。"""
        app = app or self._get_screener_app()
        # 未指定日期时只保证最近约 400 个自然日（覆盖 252 交易日 Beta 窗口），
        # 避免每次都尝试回补指数早期缺失区间。
        default_start = self.settings.get("benchmark_start_date") or (
            date.fromisoformat(today_str()) - timedelta(days=400)
        ).isoformat()
        start, end = parse_date_range(
            start_date,
            end_date,
            default_start=default_start,
        )
        code = app.settings.benchmark_code
        report = app.service.market.ensure_benchmark(start, end)
        done = bool(report.complete)
        self.state.save(
            TASK_NAME,
            BENCHMARK_STATE,
            cursor=end.isoformat(),
            status="DONE" if done else "PARTIAL",
            total=1,
            processed=1 if done else 0,
            message=None if done else "基准指数存在缺口",
            payload={
                "code": code,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        return {
            "dataset": "benchmark",
            "code": code,
            "done": done,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "coverage": report.to_dict(),
        }

    # ------------------------------------------------------------------ Screener
    def sync_screener(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        codes: Sequence[str] | str | None = None,
        force: bool = False,
        app: Any | None = None,
    ) -> dict[str, Any]:
        """构建/修复指定日期范围的日频因子快照。"""
        from screener.domain.models import BuildRequest

        app = app or self._get_screener_app()
        selected_codes = normalize_selected_codes(codes)
        skipped_codes: list[str] = []
        if selected_codes is not None:
            a_share_codes = [code for code in selected_codes if is_a_share_code(code)]
            skipped_codes = [
                code for code in selected_codes if code not in set(a_share_codes)
            ]
            selected_codes = a_share_codes
            if not selected_codes:
                return {
                    "dataset": "screener",
                    "codes": [],
                    "done": True,
                    "status": "SKIPPED",
                    "reason": "指定的代码中没有 A 股，Screener 不构建 ETF/LOF",
                    "skipped_codes": skipped_codes,
                    "rows_written": 0,
                }

        if start_date is None and end_date is None:
            target = app.baseline_service.calendar.latest_complete_trading_day()
            start = end = target
        else:
            start, end = parse_date_range(start_date, end_date)

        request = BuildRequest(
            dataset="screener_daily",
            start=start,
            end=end,
            codes=selected_codes,
            force=force,
            formula_version=app.settings.daily.formula_version,
        )
        state = app.orchestrator.ensure_job(
            request,
            universe_key=request.universe_key,
            universe_payload=request.universe_payload,
            force=force,
        )
        status = state.status.value
        done = status in ("DONE", "PARTIAL")
        self.state.save(
            TASK_NAME,
            SCREENER_STATE,
            cursor=state.build_id,
            status=status,
            total=state.requested_codes,
            processed=state.built_codes,
            message=state.message,
            payload={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "codes": selected_codes,
                "force": bool(force),
            },
        )
        return {
            "dataset": "screener",
            "codes": selected_codes,
            "skipped_codes": skipped_codes,
            "done": done,
            "build_id": state.build_id,
            "status": status,
            "quality": state.quality.value,
            "coverage_ratio": state.coverage_ratio,
            "rows_written": state.rows_written,
            "requested_codes": state.requested_codes,
            "built_codes": state.built_codes,
            "window_start": state.window_start.isoformat()
            if state.window_start
            else None,
            "window_end": state.window_end.isoformat() if state.window_end else None,
            "message": state.message,
        }

    # ------------------------------------------------------------------ 分档
    def sync_moneyflow(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """按交易日回补/增量分档数据。"""
        start = str(
            start_date
            or self.settings.get(
                "moneyflow_start_date",
                self.settings.get("daily_start_date", "2020-01-01"),
            )
        )[:10]
        end = str(end_date or today_str())[:10]
        previous = None if reset else self.state.get(TASK_NAME, MONEYFLOW_STATE)
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}
        if old_payload.get("start_date") and old_payload["start_date"] != start:
            previous = None
            old_payload = {}
        current_cursor = cursor or (previous or {}).get("cursor")
        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        attempts: dict[str, int] = dict(old_payload.get("attempts") or {})
        pending_days = [
            day
            for day in (old_payload.get("pending") or [])
            if attempts.get(day, 0) < retry_limit
        ]

        result = self._get_moneyflow_service().sync(
            start,
            end,
            max_trade_days=max_trade_days or 20,
            cursor=current_cursor,
            pending=pending_days,
        )
        new_pending = list(result.get("pending") or [])
        for day in new_pending:
            attempts[day] = attempts.get(day, 0) + 1
        for day in list(attempts):
            if day not in new_pending:
                attempts.pop(day)
        self.state.save(
            TASK_NAME,
            MONEYFLOW_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("total_days") or 0),
            processed=len(result.get("processed_days") or []),
            message=f"rows={result.get('rows_written', 0)} pending={len(new_pending)}",
            payload={
                "start_date": start,
                "end_date": end,
                "pending": new_pending,
                "attempts": attempts,
            },
        )
        result.update({"start_date": start, "end_date": end})
        return result

    # ------------------------------------------------------------------ 概念
    def sync_concept_meta(self) -> dict[str, Any]:
        result = self._get_concept_service().sync_boards()
        self.state.save(
            TASK_NAME,
            CONCEPT_META_STATE,
            cursor=None,
            status="DONE" if result.get("done") else "ERROR",
            total=int(result.get("rows_written") or 0),
            processed=int(result.get("rows_written") or 0),
            message=result.get("source"),
            payload={"as_of": result.get("as_of")},
        )
        return result

    def sync_concept_daily(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        start = str(start_date or (now_cn().date() - timedelta(days=30)))[:10]
        end = str(end_date or today_str())[:10]
        previous = None if reset else self.state.get(TASK_NAME, CONCEPT_DAILY_STATE)
        current_cursor = cursor or (previous or {}).get("cursor")
        result = self._get_concept_service().sync_daily(
            start,
            end,
            max_boards=max_boards or 50,
            cursor=current_cursor,
        )
        self.state.save(
            TASK_NAME,
            CONCEPT_DAILY_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("rows_written") or 0),
            processed=len(result.get("processed") or []),
            message=f"rows={result.get('rows_written', 0)}",
            payload={"start_date": start, "end_date": end},
        )
        result.update({"start_date": start, "end_date": end})
        return result

    def sync_concept_member(
        self,
        *,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        previous = None if reset else self.state.get(TASK_NAME, CONCEPT_MEMBER_STATE)
        current_cursor = cursor or (previous or {}).get("cursor")
        result = self._get_concept_service().sync_members(
            max_boards=max_boards,
            cursor=current_cursor,
            as_of=as_of,
        )
        self.state.save(
            TASK_NAME,
            CONCEPT_MEMBER_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("rows_written") or 0),
            processed=len(result.get("sources") or {}),
            message=f"rows={result.get('rows_written', 0)}",
            payload={"as_of": result.get("as_of")},
        )
        return result

    def sync_concept_classification(self) -> dict[str, Any]:
        result = self._get_concept_service().sync_classification()
        self.state.save(
            TASK_NAME,
            CONCEPT_CLASS_STATE,
            cursor=None,
            status="DONE" if result.get("done") else "ERROR",
            total=int(result.get("rows_written") or 0),
            processed=int(result.get("rows_written") or 0),
            message=None if result.get("done") else str(result.get("errors")),
            payload={"as_of": result.get("as_of")},
        )
        return result

    # ------------------------------------------------------------------ 编排
    def run(
        self,
        *,
        dataset: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
        codes: Sequence[str] | str | None = None,
        max_trade_days: int | None = None,
        max_stocks: int | None = None,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        include_funds: bool | None = None,
        include_benchmark: bool = True,
        include_screener: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """按阶段编排一次股票数据更新。

        - ``dataset="all"``：日线 → 基准 → 财务 → Screener → 分档 → 板块列表；
        - ``codes`` 为空：全市场；指定后只更新这些代码；
        - ``start_date`` / ``end_date``：日线/基准/Screener/分档用交易日，
          财务用报告年份 + 估值日期范围。
        """
        dataset = dataset.lower()
        if dataset not in DATASETS:
            raise ValueError(f"dataset 必须是 {' / '.join(DATASETS)}")

        selected_codes = normalize_selected_codes(codes)
        # 综合更新默认包含估值；财务单独调用时仍可显式关闭。
        if include_valuation is None:
            include_valuation = True

        result: dict[str, Any] = {
            "dataset": dataset,
            "codes": selected_codes,
            "start_date": start_date,
            "end_date": end_date,
        }

        need_app = (
            (dataset == "benchmark" and include_benchmark)
            or (dataset == "screener" and include_screener)
            or (dataset == "all" and (include_benchmark or include_screener))
        )
        app = self._get_screener_app() if need_app else None

        if dataset in ("daily", "all"):
            result["daily"] = self.sync_daily(
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                cursor=cursor if dataset == "daily" else None,
                reset=reset,
                include_funds=include_funds,
                codes=selected_codes,
            )
        if dataset in ("benchmark", "all"):
            if include_benchmark:
                result["benchmark"] = self.sync_benchmark(
                    start_date=start_date,
                    end_date=end_date,
                    app=app,
                )
            else:
                result["benchmark"] = {
                    "dataset": "benchmark",
                    "done": True,
                    "status": "SKIPPED",
                }
        if dataset in ("financial", "all"):
            result["financial"] = self.sync_financials(
                start_date=start_date,
                end_date=end_date,
                max_stocks=max_stocks,
                cursor=cursor if dataset == "financial" else None,
                reset=reset,
                include_valuation=include_valuation,
                codes=selected_codes,
                force=force,
            )
        if dataset in ("screener", "all"):
            if include_screener:
                result["screener"] = self.sync_screener(
                    start_date=start_date,
                    end_date=end_date,
                    codes=selected_codes,
                    force=force,
                    app=app,
                )
            else:
                result["screener"] = {
                    "dataset": "screener",
                    "done": True,
                    "status": "SKIPPED",
                }
        if dataset in ("moneyflow", "all"):
            result["moneyflow"] = self.sync_moneyflow(
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                cursor=cursor if dataset == "moneyflow" else None,
                reset=reset,
            )
        if dataset in ("concept_meta", "all"):
            result["concept_meta"] = self.sync_concept_meta()
        if dataset == "concept_daily":
            result["concept_daily"] = self.sync_concept_daily(
                start_date=start_date,
                end_date=end_date,
                max_boards=max_boards,
                cursor=cursor,
                reset=reset,
            )
        if dataset == "concept_member":
            result["concept_member"] = self.sync_concept_member(
                max_boards=max_boards,
                cursor=cursor,
                reset=reset,
            )
        if dataset == "concept_class":
            result["concept_class"] = self.sync_concept_classification()

        phase_keys = [
            key
            for key in (
                "daily",
                "benchmark",
                "financial",
                "screener",
                "moneyflow",
                "concept_meta",
                "concept_daily",
                "concept_member",
                "concept_class",
            )
            if key in result
        ]
        result["done"] = all(result[key].get("done", True) for key in phase_keys)
        result["next_cursors"] = {
            key: result[key].get("next_cursor")
            for key in ("daily", "financial", "moneyflow", "concept_daily", "concept_member")
            if key in result
        }
        if dataset == "daily":
            result["next_cursor"] = result["daily"].get("next_cursor")
        elif dataset == "financial":
            result["next_cursor"] = result["financial"].get("next_cursor")
        elif dataset in ("moneyflow", "concept_daily", "concept_member"):
            result["next_cursor"] = result[dataset].get("next_cursor")
        elif dataset == "all":
            if not result["daily"].get("done"):
                result["next_cursor"] = result["daily"].get("next_cursor")
            else:
                result["next_cursor"] = result["financial"].get("next_cursor")
        else:
            result["next_cursor"] = None
        return result

    def status(self) -> dict[str, Any]:
        latest = self.state.list_latest(TASK_NAME)
        return {
            "daily": latest.get(DAILY_STATE),
            "daily_codes": latest.get(DAILY_CODES_STATE),
            "financial": latest.get(FINANCIAL_STATE),
            "financial_codes": latest.get(FINANCIAL_CODES_STATE),
            "benchmark": latest.get(BENCHMARK_STATE),
            "screener": latest.get(SCREENER_STATE),
            "moneyflow": latest.get(MONEYFLOW_STATE),
            "concept_meta": latest.get(CONCEPT_META_STATE),
            "concept_daily": latest.get(CONCEPT_DAILY_STATE),
            "concept_member": latest.get(CONCEPT_MEMBER_STATE),
            "concept_class": latest.get(CONCEPT_CLASS_STATE),
        }
