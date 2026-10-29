"""披露日历 / 公告仓储（QuestDB）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

logger = logging.getLogger("DataInterface.Disclosure.Repository")

_CALENDAR_TABLE = '"fin_disclosure_calendar"'
_ANNOUNCEMENT_TABLE = '"announcement"'

CALENDAR_COLUMNS = [
    "ts",
    "code",
    "report_date",
    "report_type",
    "appoint_date",
    "first_appoint_date",
    "first_change_date",
    "second_change_date",
    "third_change_date",
    "actual_date",
    "residual_days",
    "is_published",
    "market",
    "source",
    "fetched_at",
]

ANNOUNCEMENT_COLUMNS = [
    "ts",
    "code",
    "art_code",
    "title",
    "ann_type",
    "column_code",
    "column_name",
    "display_time",
    "source",
    "fetched_at",
]


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).to_pydatetime()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def _rows(frame: pd.DataFrame, columns: list[str]) -> list[tuple]:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"披露数据缺少列: {missing}")
    return [tuple(_clean(row[column]) for column in columns) for _, row in frame.iterrows()]


def _day(value: str | date | datetime) -> datetime:
    return datetime.combine(pd.Timestamp(value).date(), time.min)


class DisclosureRepository:
    def __init__(self, db: QuestDBClient | None = None, config: dict[str, Any] | None = None):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)

    # ------------------------------------------------------------------ 写入
    def upsert_calendar(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_CALENDAR_TABLE} ({', '.join(CALENDAR_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, CALENDAR_COLUMNS), page_size=500)

    def upsert_announcements(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_ANNOUNCEMENT_TABLE} ({', '.join(ANNOUNCEMENT_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, ANNOUNCEMENT_COLUMNS), page_size=500)

    # ------------------------------------------------------------------ 查询
    def calendar(
        self,
        code: str | None = None,
        *,
        as_of: str | date | None = None,
        report_date: str | date | None = None,
    ) -> pd.DataFrame:
        """取每 ``(code, report_date)`` 的最新预约快照。"""
        # QuestDB 的 ``LATEST ON`` 后不能再直接跟 ``WHERE``，必须包一层子查询；
        # 且语义上也必须「先取每个 (code, report_date) 的最新快照，再过滤」。
        sql = (
            "SELECT * FROM ("
            f"SELECT * FROM {_CALENDAR_TABLE} "
            "LATEST ON ts PARTITION BY code, report_date"
            ") "
        )
        conditions: list[str] = []
        params: list[Any] = []
        if code:
            conditions.append("code = %s")
            params.append(str(code))
        if report_date:
            conditions.append("report_date = %s")
            params.append(_day(report_date))
        if as_of:
            conditions.append("ts <= %s")
            params.append(_day(as_of))
        if conditions:
            sql += "WHERE " + " AND ".join(conditions) + " "
        sql += "ORDER BY code"
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("披露日历查询失败: %s", exc)
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        return frame.reset_index(drop=True)

    def upcoming(self, days: int = 30, *, as_of: str | date | None = None) -> pd.DataFrame:
        """未来 ``days`` 天内待披露（且尚未披露）的公司。"""
        anchor = _day(as_of) if as_of else _day(datetime.now().date())
        sql = (
            "SELECT * FROM ("
            f"SELECT * FROM {_CALENDAR_TABLE} "
            "LATEST ON ts PARTITION BY code, report_date"
            ") "
            "WHERE actual_date IS NULL AND appoint_date IS NOT NULL "
            "AND appoint_date >= %s AND appoint_date <= dateadd('d', %s, %s) "
            "ORDER BY appoint_date, code"
        )
        params: list[Any] = [anchor, int(days), anchor]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("待披露查询失败: %s", exc)
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=CALENDAR_COLUMNS)
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        return frame.reset_index(drop=True)

    def announcements(
        self,
        code: str | None = None,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        sql = f"SELECT * FROM {_ANNOUNCEMENT_TABLE}"
        conditions: list[str] = []
        params: list[Any] = []
        if code:
            conditions.append("code = %s")
            params.append(str(code))
        if start:
            conditions.append("ts >= %s")
            params.append(_day(start))
        if end:
            conditions.append("ts <= %s")
            params.append(datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0))
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY ts DESC LIMIT %s"
        params.append(int(limit))
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("公告查询失败: %s", exc)
            return pd.DataFrame(columns=ANNOUNCEMENT_COLUMNS)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=ANNOUNCEMENT_COLUMNS)
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        return frame.reset_index(drop=True)
