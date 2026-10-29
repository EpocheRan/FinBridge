"""披露日历与公告服务：同步编排 + 查询。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

from .repository import DisclosureRepository
from .sources.em_source import EmDisclosureSource

logger = logging.getLogger("DataInterface.Disclosure.Service")


def default_report_dates(today: date | None = None, periods: int = 4) -> list[str]:
    """最近 ``periods`` 个报告期（季末日期），按时间倒序。"""
    today = today or now_cn().date()
    candidates: list[str] = []
    for year in (today.year, today.year - 1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            period = date(year, month, day)
            if period <= today:
                candidates.append(period.isoformat())
    return sorted(set(candidates), reverse=True)[:periods]


class DisclosureService:
    def __init__(
        self,
        repository: DisclosureRepository | None = None,
        source: EmDisclosureSource | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        settings = self.config.get("disclosure", {}) or {}
        self.repository = repository or DisclosureRepository(config=self.config)
        self.source = source or EmDisclosureSource(
            page_size=int(settings.get("page_size", 500)),
            max_pages=int(settings.get("max_pages", 1200)),
            page_delay=float(settings.get("page_delay_seconds", 0.15)),
        )
        self.announcement_page_size = int(settings.get("announcement_page_size", 100))
        self.announcement_max_pages = int(settings.get("announcement_max_pages", 5))

    # ------------------------------------------------------------------ 日历
    def sync_calendar(
        self,
        report_dates: Sequence[str] | None = None,
        *,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        dates = list(report_dates or default_report_dates())
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        written = 0
        details: list[dict[str, Any]] = []
        for report_date in dates:
            frame = self.source.fetch_calendar(report_date)
            if frame is None or frame.empty:
                details.append({"report_date": report_date, "rows": 0, "pages": 0})
                continue
            frame = frame.copy()
            frame["ts"] = snapshot
            frame["source"] = self.source.source_id
            frame["fetched_at"] = now_cn()
            for column in ("report_type", "market"):
                if column not in frame.columns:
                    frame[column] = None
            count = self.repository.upsert_calendar(frame)
            written += count
            details.append(
                {
                    "report_date": report_date,
                    "rows": count,
                    "pages": frame.attrs.get("pages", 0),
                    "appoint_min": str(frame["appoint_date"].min())[:10],
                    "appoint_max": str(frame["appoint_date"].max())[:10],
                }
            )
            logger.info("报告期 %s 写入 %s 行", report_date, count)
        return {
            "dataset": "disclosure_calendar",
            "as_of": str(snapshot.date()),
            "report_dates": dates,
            "rows_written": written,
            "details": details,
        }

    def sync_calendar_for_stock(self, code: str, *, as_of: str | date | None = None) -> dict[str, Any]:
        frame = self.source.fetch_calendar_for_stock(code)
        if frame is None or frame.empty:
            return {"dataset": "disclosure_calendar", "code": normalize_code(code), "rows_written": 0}
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        frame = frame.copy()
        frame["ts"] = snapshot
        frame["source"] = self.source.source_id
        frame["fetched_at"] = now_cn()
        for column in ("report_type", "market"):
            if column not in frame.columns:
                frame[column] = None
        written = self.repository.upsert_calendar(frame)
        return {"dataset": "disclosure_calendar", "code": code, "rows_written": written}

    def calendar(self, code: str | None = None, *, as_of: str | date | None = None) -> pd.DataFrame:
        return self.repository.calendar(code, as_of=as_of)

    def upcoming(self, days: int = 30, *, as_of: str | date | None = None) -> pd.DataFrame:
        return self.repository.upcoming(days, as_of=as_of)

    # ------------------------------------------------------------------ 公告
    def sync_announcements(
        self, codes: Sequence[str], *, max_pages: int | None = None
    ) -> dict[str, Any]:
        written = 0
        errors: list[dict[str, str]] = []
        for code in codes:
            code = normalize_code(code)
            if not code:
                continue
            frame = self.source.fetch_announcements(
                code,
                page_size=self.announcement_page_size,
                max_pages=max_pages or self.announcement_max_pages,
            )
            if frame is None or frame.empty:
                errors.append({"code": code, "error": "empty"})
                continue
            frame = frame.copy()
            frame["ts"] = frame["notice_date"]
            frame["source"] = self.source.source_id
            frame["fetched_at"] = now_cn()
            written += self.repository.upsert_announcements(frame)
        return {
            "dataset": "announcement",
            "codes": len(codes),
            "rows_written": written,
            "errors": errors,
        }

    def announcements(
        self,
        code: str | None = None,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        return self.repository.announcements(code, start=start, end=end, limit=limit)

    def upcoming_for_stock(self, code: str, days: int = 30) -> dict[str, Any] | None:
        """某只股票在 ``days`` 天内的待披露事项（含剩余天数）。"""
        frame = self.calendar(code)
        if frame.empty:
            return None
        today = pd.Timestamp(now_cn().date())
        pending = frame[frame["actual_date"].isna()].copy()
        if pending.empty:
            return None
        pending["days_to"] = (pd.to_datetime(pending["appoint_date"]) - today).dt.days
        pending = pending.sort_values("days_to")
        row = pending.iloc[0]
        return {
            "code": str(row["code"]),
            "report_date": str(pd.Timestamp(row["report_date"]).date()),
            "report_type": row.get("report_type"),
            "appoint_date": str(pd.Timestamp(row["appoint_date"]).date())
            if pd.notna(row.get("appoint_date"))
            else None,
            "days_to_disclosure": int(row["days_to"]) if pd.notna(row["days_to"]) else None,
            "rescheduled": int(
                sum(
                    pd.notna(row.get(column))
                    for column in ("first_change_date", "second_change_date", "third_change_date")
                )
            ),
        }
