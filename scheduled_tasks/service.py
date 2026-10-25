"""A 股全量日线/财务数据同步任务。

模块本身不负责定时，只提供可被 Agent 心跳反复调用的「有界批次」：

- 日线：按交易日批量拉取全市场（一次 Tushare daily 返回全市场），
  可选同时拉 ETF/LOF（``include_funds``）；
- 财务：按股票池分批，逐只拉取三大报表和派生指标；
- 每次只处理有限数量，返回 next_cursor，下一次心跳从游标继续。

2026-10 修订：

- 日线写入 **不复权价 + adj_factor**（按交易日一次取全市场因子）；
- 某交易日拿不到数据时不再直接跳过：记 ``pending`` 并在后续心跳重试，
  超过上限才放弃并记 error，避免永久缺口；
- 支持 ``include_funds``（ETF/LOF 日线 + 基金基础信息）。
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import is_a_share_code, normalize_code
from data_interface.common.timeutil import market_closed_today, now_cn, today_str
from data_interface.common.trading_calendar import TradingCalendar
from data_interface.financial.service import FinancialDataService
from data_interface.financial.sources.tushare_source import (
    TushareSource,
    get_shared_tushare_source,
)
from data_interface.market.adj_factors import AdjFactorProvider
from data_interface.market.fetcher import MarketDataFetcher

from .state import TaskStateRepository

logger = logging.getLogger("ScheduledTasks.AShareSync")

TASK_NAME = "a_share_sync"
STOP_ERRORS = (
    RateLimitExceededError,
    DailyQuotaExceededError,
    PermissionDeniedError,
    SourceUnavailableError,
)


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
    ) -> dict[str, Any]:
        include_funds = (
            bool(self.settings.get("include_funds", True))
            if include_funds is None
            else bool(include_funds)
        )
        start_date = start_date or self.settings.get("daily_start_date", "2020-01-01")
        end_date = end_date or today_str()
        max_trade_days = max(1, int(max_trade_days or self.settings.get("daily_batch_days", 20)))
        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        payload: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "include_funds": include_funds,
        }

        previous = None if reset else self.state.get(TASK_NAME, "daily")
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}
        # 起始日期或基金口径变化视为新任务
        if old_payload and (
            old_payload.get("start_date") != start_date
            or bool(old_payload.get("include_funds")) != include_funds
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
                TASK_NAME, "daily", cursor=None, status="DONE", total=0, processed=0, payload=payload
            )
            return {
                "dataset": "daily",
                "done": True,
                "total": 0,
                "processed": 0,
                "next_cursor": None,
                "rows_written": 0,
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
                "daily",
                cursor=current_cursor,
                status="DONE",
                total=len(all_days),
                processed=len(all_days),
                payload=payload,
            )
            return {
                "dataset": "daily",
                "done": True,
                "total": len(all_days),
                "processed": len(all_days),
                "next_cursor": None,
                "rows_written": 0,
                "errors": [],
            }

        batch = days[:max_trade_days]
        rows_written = 0
        fund_rows_written = 0
        errors: list[str] = []
        paused_at: str | None = None

        if include_funds:
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
                frame = self._attach_factors(frame, trade_date)
                if not frame.empty:
                    rows_written += self.market_fetcher.update_ohlc(frame, "d")

                if include_funds:
                    fund_frame = self._fund_daily_frame(
                        self.tushare.fetch_fund_daily_all(trade_date)
                    )
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
                    "daily",
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
                    "daily",
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
            "daily",
            cursor=state_cursor,
            status="DONE" if done else ("PAUSED" if paused_at else "RUNNING"),
            total=len(all_days),
            processed=processed,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "daily",
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
    ) -> dict[str, Any]:
        start_year = (
            datetime.strptime(start_date, "%Y-%m-%d").year
            if start_date
            else int(self.settings.get("financial_start_year", 2020))
        )
        end_year = (
            datetime.strptime(end_date, "%Y-%m-%d").year if end_date else now_cn().year
        )
        max_stocks = max(1, int(max_stocks or self.settings.get("financial_batch_stocks", 15)))
        include_valuation = (
            bool(self.settings.get("include_valuation", False))
            if include_valuation is None
            else include_valuation
        )
        payload = {
            "start_year": start_year,
            "end_year": end_year,
            "include_valuation": include_valuation,
        }

        previous = None if reset else self.state.get(TASK_NAME, "financial")
        if previous and previous.get("payload"):
            try:
                old = json.loads(previous["payload"])
            except Exception:
                old = {}
            if (
                old.get("start_year") != start_year
                or old.get("end_year") != end_year
                or bool(old.get("include_valuation")) != include_valuation
            ):
                previous = None

        if previous and previous.get("status") == "DONE" and not reset and not cursor:
            return {
                "dataset": "financial",
                "done": True,
                "total": previous.get("total") or 0,
                "processed": previous.get("processed") or 0,
                "next_cursor": None,
                "stocks_written": 0,
                "errors": [],
            }

        universe = self.financial.ensure_stock_universe(
            int(self.settings.get("min_universe_size", 1000))
        )
        universe = sorted(code for code in universe if is_a_share_code(code))
        if not universe:
            raise RuntimeError("A 股股票池为空，无法同步财务数据")

        current_cursor = cursor or (previous or {}).get("cursor")
        start_index = bisect_left(universe, current_cursor) if current_cursor else 0
        batch = universe[start_index : start_index + max_stocks]
        if not batch:
            self.state.save(
                TASK_NAME,
                "financial",
                cursor=None,
                status="DONE",
                total=len(universe),
                processed=len(universe),
                payload=payload,
            )
            return {
                "dataset": "financial",
                "done": True,
                "total": len(universe),
                "processed": len(universe),
                "next_cursor": None,
                "stocks_written": 0,
                "errors": [],
            }

        errors: list[str] = []
        stocks_written = 0
        for offset, code in enumerate(batch, start=start_index):
            try:
                self.financial.sync_statements(
                    code, start_year=start_year, end_year=end_year, force=False
                )
                self.financial.sync_indicators(code)
                if include_valuation:
                    self.financial.sync_valuation(
                        code,
                        start_date=f"{start_year}-01-01",
                        end_date=f"{end_year}-12-31",
                        force=False,
                    )
                stocks_written += 1
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    "financial",
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
            "financial",
            cursor=next_cursor,
            status="DONE" if done else "RUNNING",
            total=len(universe),
            processed=next_index,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "financial",
            "done": done,
            "total": len(universe),
            "processed": next_index,
            "next_cursor": next_cursor,
            "stocks_written": stocks_written,
            "errors": errors[:10],
        }

    # ------------------------------------------------------------------ 编排
    def run(
        self,
        *,
        dataset: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        max_stocks: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        include_funds: bool | None = None,
    ) -> dict[str, Any]:
        dataset = dataset.lower()
        if dataset not in ("daily", "financial", "all"):
            raise ValueError("dataset 必须是 daily / financial / all")

        result: dict[str, Any] = {"dataset": dataset}
        if dataset in ("daily", "all"):
            result["daily"] = self.sync_daily(
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                cursor=cursor if dataset == "daily" else None,
                reset=reset,
                include_funds=include_funds,
            )
        if dataset in ("financial", "all"):
            result["financial"] = self.sync_financials(
                start_date=start_date,
                end_date=end_date,
                max_stocks=max_stocks,
                cursor=cursor if dataset == "financial" else None,
                reset=reset,
                include_valuation=include_valuation,
            )
        result["done"] = all(
            item.get("done", True)
            for key, item in result.items()
            if key in ("daily", "financial")
        )
        return result

    def status(self) -> dict[str, Any]:
        return {
            "daily": self.state.get(TASK_NAME, "daily"),
            "financial": self.state.get(TASK_NAME, "financial"),
        }
