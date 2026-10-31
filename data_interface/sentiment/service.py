"""情绪数据同步服务：两融 / 龙虎榜 / 大宗交易。

按交易日批量拉取（一次一个交易日、全市场），写入 ``margin_detail`` /
``top_list`` / ``block_trade``。游标与 pending 语义与 moneyflow 保持一致：
调用方负责把 ``pending`` 与 ``attempts`` 落进任务状态表。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Iterable, Sequence

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import today_str
from data_interface.common.trading_calendar import TradingCalendar

from .repository import SentimentRepository

logger = logging.getLogger("DataInterface.Sentiment.Service")

STOP_ERRORS = (
    RateLimitExceededError,
    DailyQuotaExceededError,
    PermissionDeniedError,
    SourceUnavailableError,
)

#: dataset → TushareSource 方法名
FETCHERS = {
    "margin_detail": "fetch_margin_detail",
    "top_list": "fetch_top_list",
    "block_trade": "fetch_block_trade",
}


def _code(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    if not text:
        return None
    code = normalize_code(text.split(".")[0])
    return code if len(code) == 6 else None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _text(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    return text[:limit] if text and text.lower() != "nan" else None


def _ts(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), datetime.min.time())


def map_margin_detail(raw: Iterable[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    ts = _ts(day)
    rows: list[dict[str, Any]] = []
    for item in raw or []:
        code = _code(item.get("ts_code"))
        if not code:
            continue
        rows.append(
            {
                "ts": ts,
                "code": code,
                "rzye": _num(item.get("rzye")),
                "rqye": _num(item.get("rqye")),
                "rzmre": _num(item.get("rzmre")),
                "rqyl": _num(item.get("rqyl")),
                "rzche": _num(item.get("rzche")),
                "rqchl": _num(item.get("rqchl")),
            }
        )
    return rows


def map_top_list(raw: Iterable[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    ts = _ts(day)
    rows: list[dict[str, Any]] = []
    for item in raw or []:
        code = _code(item.get("ts_code"))
        if not code:
            continue
        rows.append(
            {
                "ts": ts,
                "code": code,
                "name": _text(item.get("name")),
                "close": _num(item.get("close")),
                "pct_chg": _num(item.get("pct_change")),
                "turnover_rate": _num(item.get("turnover_rate")),
                "amount": _num(item.get("amount")),
                "l_sell": _num(item.get("l_sell")),
                "l_buy": _num(item.get("l_buy")),
                "l_amount": _num(item.get("l_amount")),
                "net_amount": _num(item.get("net_amount")),
                "reason": _text(item.get("reason")),
            }
        )
    return rows


def map_block_trade(raw: Iterable[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    ts = _ts(day)
    rows: list[dict[str, Any]] = []
    for item in raw or []:
        code = _code(item.get("ts_code"))
        if not code:
            continue
        rows.append(
            {
                "ts": ts,
                "code": code,
                "price": _num(item.get("price")),
                "vol": _num(item.get("vol")),
                "amount": _num(item.get("amount")),
                "buyer": _text(item.get("buyer")),
                "seller": _text(item.get("seller")),
            }
        )
    return rows


MAPPERS: dict[str, Callable[[Iterable[dict[str, Any]], str], list[dict[str, Any]]]] = {
    "margin_detail": map_margin_detail,
    "top_list": map_top_list,
    "block_trade": map_block_trade,
}


def _should_retry(day: str) -> bool:
    """只对最近 3 天的空数据重试，避免历史空日（如当日无大宗）永久挂起。"""
    try:
        age = (date.fromisoformat(today_str()) - date.fromisoformat(day)).days
    except ValueError:
        return False
    return 0 <= age <= 3


class SentimentService:
    def __init__(
        self,
        *,
        repository: SentimentRepository | None = None,
        tushare: Any | None = None,
        config: dict[str, Any] | None = None,
        trading_calendar: TradingCalendar | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("sentiment", {}) or {}
        self.repository = repository or SentimentRepository(config=self.config)
        if tushare is None:
            from data_interface.financial.sources.tushare_source import (
                get_shared_tushare_source,
            )

            tushare = get_shared_tushare_source(self.config)
        self.tushare = tushare
        self._calendar = trading_calendar

    @property
    def calendar(self) -> TradingCalendar:
        if self._calendar is None:
            self._calendar = TradingCalendar()
        return self._calendar

    def latest_ts(self, dataset: str):
        return self.repository.latest_ts(dataset)

    def sync(
        self,
        dataset: str,
        start_date: str | date,
        end_date: str | date,
        *,
        max_trade_days: int = 10,
        cursor: str | None = None,
        pending: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if dataset not in FETCHERS:
            raise ValueError(
                f"dataset 必须是 {sorted(FETCHERS)} 之一，收到 {dataset!r}"
            )
        start_text = str(start_date)[:10]
        end_text = str(end_date)[:10]
        all_days = [
            str(day)[:10]
            for day in self.calendar.trading_days_or_weekdays(start_text, end_text)
        ]
        pending_days = sorted(
            {
                str(day)[:10]
                for day in (pending or [])
                if start_text <= str(day)[:10] <= end_text
            }
        )
        calendar_days = [day for day in all_days if not cursor or day >= cursor]
        days = sorted(set(pending_days) | set(calendar_days))
        batch = days[: max(1, int(max_trade_days))]

        fetch = getattr(self.tushare, FETCHERS[dataset])
        mapper = MAPPERS[dataset]
        rows_written = 0
        processed: list[str] = []
        retry_pending: list[str] = []
        empty_days: list[str] = []
        errors: list[dict[str, str]] = []

        for day in batch:
            try:
                raw = fetch(day.replace("-", ""))
            except STOP_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 拉取 %s 失败: %s", dataset, day, exc)
                errors.append({"date": day, "error": str(exc)[:200]})
                if _should_retry(day):
                    retry_pending.append(day)
                processed.append(day)
                continue

            rows = mapper(raw, day)
            if not rows:
                empty_days.append(day)
                if _should_retry(day):
                    retry_pending.append(day)
                processed.append(day)
                continue

            rows_written += self.repository.upsert(dataset, rows)
            processed.append(day)

        remaining = days[len(batch) :]
        return {
            "dataset": dataset,
            "done": not remaining,
            "next_cursor": remaining[0] if remaining else None,
            "rows_written": rows_written,
            "processed_days": processed,
            "pending": retry_pending,
            "empty_days": empty_days,
            "errors": errors,
            "total_days": len(days),
        }
