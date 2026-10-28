"""分档数据服务：同步编排、质量标记、查询聚合。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.trading_calendar import TradingCalendar

from .models import CANONICAL_COLUMNS, MoneyflowSourceMeta, RANK_FIELDS
from .repository import MoneyflowRepository
from .sources.tushare_source import TushareMoneyflowSource

logger = logging.getLogger("DataInterface.Moneyflow.Service")


class MoneyflowService:
    def __init__(
        self,
        *,
        repository: MoneyflowRepository | None = None,
        sources: Sequence[Any] | None = None,
        config: dict[str, Any] | None = None,
        trading_calendar: TradingCalendar | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("moneyflow", {}) or {}
        self.repository = repository or MoneyflowRepository(config=self.config)
        self.source_adapters = {
            source.source_id: source
            for source in (
                sources
                if sources is not None
                else [TushareMoneyflowSource(self.config)]
            )
        }
        self.priority = list(
            self.settings.get("source_priority")
            or [source for source in self.source_adapters]
        )
        self.min_rows = int(self.settings.get("quality_min_rows", 1000))
        self._calendar = trading_calendar

    # ------------------------------------------------------------------ 源
    @property
    def calendar(self) -> TradingCalendar:
        if self._calendar is None:
            self._calendar = TradingCalendar()
        return self._calendar

    def ensure_source_meta(self) -> int:
        """把各源的口径写进 ``moneyflow_source``（幂等）。"""
        written = 0
        for source in self.source_adapters.values():
            meta: MoneyflowSourceMeta | None = getattr(source, "meta", None)
            if meta is None:
                continue
            written += self.repository.upsert_source_meta(meta.to_payload())
        return written

    def _ordered_sources(self) -> list[Any]:
        ordered = [
            self.source_adapters[item]
            for item in self.priority
            if item in self.source_adapters
        ]
        for source_id, source in self.source_adapters.items():
            if source not in ordered:
                ordered.append(source)
        return ordered

    # ------------------------------------------------------------------ 同步
    def sync(
        self,
        start_date: str | date,
        end_date: str | date,
        *,
        max_trade_days: int = 20,
        cursor: str | None = None,
        pending: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """按交易日批量拉取分档数据。

        返回结构与 ``acquisition`` 的其它数据集保持一致：
        ``done / next_cursor / rows_written / processed_days / errors / pending``。

        ``pending`` 用于重试上次没拿到数据的交易日（例如当天尚未发布）。
        """
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

        rows_written = 0
        processed: list[str] = []
        errors: list[dict[str, str]] = []
        retry_pending: list[str] = []
        source_counts: dict[str, int] = {}

        for day in batch:
            frame = pd.DataFrame(columns=CANONICAL_COLUMNS)
            used_source = ""
            last_error: Exception | None = None
            for source in self._ordered_sources():
                try:
                    candidate = source.fetch_by_date(day)
                except Exception as exc:  # 源不可用/限流：换下一个源
                    last_error = exc
                    logger.warning("分档源 %s 拉取 %s 失败: %s", source.source_id, day, exc)
                    continue
                if candidate is not None and not candidate.empty:
                    frame = candidate
                    used_source = source.source_id
                    break
            if frame.empty:
                retry_pending.append(day)
                errors.append(
                    {
                        "date": day,
                        "error": str(last_error) if last_error else "empty",
                    }
                )
                processed.append(day)
                continue

            quality, note = self._quality(frame)
            frame = frame.copy()
            frame["quality"] = quality
            frame["quality_note"] = note
            rows_written += self.repository.upsert(frame)
            source_counts[used_source] = source_counts.get(used_source, 0) + len(frame)
            processed.append(day)

        remaining = days[len(batch) :]
        return {
            "dataset": "moneyflow",
            "done": not remaining,
            "next_cursor": remaining[0] if remaining else None,
            "rows_written": rows_written,
            "processed_days": processed,
            "pending": retry_pending,
            "source_rows": source_counts,
            "errors": errors,
            "total_days": len(days),
        }

    def _quality(self, frame: pd.DataFrame) -> tuple[str, str | None]:
        if frame.empty:
            return "partial", "empty"
        if len(frame) < self.min_rows:
            return "partial", f"rows={len(frame)} < min_rows={self.min_rows}"
        amount_columns = [
            column
            for column in frame.columns
            if column.endswith("_amount") and column != "net_mf_amount"
        ]
        if amount_columns and frame[amount_columns].isna().all(axis=1).any():
            return "partial", "some rows have no bucket amount"
        return "ok", None

    # ------------------------------------------------------------------ 查询
    def resolve_source(self, source: str | None = None) -> str:
        if source:
            return source
        return self.repository.resolve_source(self.priority)

    def get_by_code(
        self,
        code: str,
        start_date: str | date,
        end_date: str | date,
        *,
        source: str | None = None,
    ) -> pd.DataFrame:
        return self.repository.query_by_code(
            code, start_date, end_date, source=self.resolve_source(source)
        )

    def get_by_date(self, trade_date: str | date, *, source: str | None = None) -> pd.DataFrame:
        return self.repository.query_by_date(
            trade_date, source=self.resolve_source(source)
        )

    def rank(
        self,
        trade_date: str | date,
        *,
        field: str = "main_amount",
        limit: int = 50,
        source: str | None = None,
        ascending: bool = False,
    ) -> pd.DataFrame:
        if field not in RANK_FIELDS:
            raise ValueError(f"未知排序字段: {field!r}，可选: {sorted(RANK_FIELDS)}")
        return self.repository.rank(
            trade_date,
            field,
            source=self.resolve_source(source),
            limit=limit,
            ascending=ascending,
        )

    def sources(self) -> pd.DataFrame:
        return self.repository.sources()

    def latest_ts(self, source: str | None = None) -> pd.Timestamp | None:
        return self.repository.latest_ts(source)
