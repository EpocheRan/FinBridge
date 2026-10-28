"""概念板块仓储（QuestDB）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

logger = logging.getLogger("DataInterface.Concept.Repository")

_BOARD_TABLE = '"concept_board_dim"'
_DAILY_TABLE = '"concept_board_daily"'
_MEMBER_TABLE = '"concept_member_snapshot"'
_CLASS_TABLE = '"concept_classification"'
_ALIAS_TABLE = '"concept_board_alias"'

BOARD_COLUMNS = [
    "ts",
    "board_code",
    "name",
    "board_type",
    "source",
    "source_code",
    "status",
    "updated_at",
]
DAILY_COLUMNS = [
    "ts",
    "board_code",
    "board_name",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "quality",
    "source",
    "fetched_at",
]
MEMBER_COLUMNS = [
    "ts",
    "board_code",
    "stock_code",
    "stock_name",
    "weight",
    "in_date",
    "quality",
    "source",
    "fetched_at",
]
CLASS_COLUMNS = [
    "ts",
    "concept_code",
    "name",
    "class1_code",
    "class1_name",
    "class2_code",
    "class2_name",
    "class3_code",
    "class3_name",
    "item_size",
    "source",
    "fetched_at",
]
ALIAS_COLUMNS = [
    "updated_at",
    "board_code",
    "source",
    "source_code",
    "source_name",
    "confidence",
    "verified",
    "notes",
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


def _rows(frame: pd.DataFrame, columns: Sequence[str]) -> list[tuple]:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"概念数据缺少列: {missing}")
    return [
        tuple(_clean(row[column]) for column in columns) for _, row in frame.iterrows()
    ]


def _day(value: str | date | datetime) -> datetime:
    return datetime.combine(pd.Timestamp(value).date(), time.min)


def _with_ts(frame: pd.DataFrame) -> pd.DataFrame:
    """``query_dataframe`` 会把 ts 设为索引；统一还原成普通列。"""
    if frame is None or frame.empty:
        return frame
    if "ts" not in frame.columns:
        frame = frame.reset_index()
    return frame


class ConceptRepository:
    def __init__(self, db: QuestDBClient | None = None, config: dict[str, Any] | None = None):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)

    # ------------------------------------------------------------------ 写入
    def upsert_boards(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_BOARD_TABLE} ({', '.join(BOARD_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, BOARD_COLUMNS), page_size=500)

    def upsert_daily(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_DAILY_TABLE} ({', '.join(DAILY_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, DAILY_COLUMNS), page_size=500)

    def upsert_members(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_MEMBER_TABLE} ({', '.join(MEMBER_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, MEMBER_COLUMNS), page_size=500)

    def upsert_classification(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_CLASS_TABLE} ({', '.join(CLASS_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, CLASS_COLUMNS), page_size=500)

    def upsert_alias(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_ALIAS_TABLE} ({', '.join(ALIAS_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, ALIAS_COLUMNS), page_size=200)

    # ------------------------------------------------------------------ 查询
    def list_boards(
        self,
        as_of: str | date | None = None,
        *,
        status: str | None = "active",
        source: str | None = None,
    ) -> pd.DataFrame:
        if as_of is None:
            sql = f"SELECT * FROM {_BOARD_TABLE}"
            params: list[Any] = []
            if source:
                sql += " WHERE source = %s"
                params.append(source)
            sql += " LATEST ON ts PARTITION BY board_code"
        else:
            snapshot = _day(as_of)
            sql = (
                f"SELECT * FROM {_BOARD_TABLE} "
                "WHERE ts = (SELECT max(ts) FROM "
                f"{_BOARD_TABLE} WHERE ts <= %s"
            )
            params = [snapshot]
            if source:
                sql += " AND source = %s"
                params.append(source)
            sql += ")"
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:
            logger.warning("板块列表查询失败: %s", exc)
            return pd.DataFrame(columns=BOARD_COLUMNS)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=BOARD_COLUMNS)
        frame = _with_ts(frame)
        if status and "status" in frame.columns:
            frame = frame[frame["status"].fillna("active") == status]
        return frame.sort_values("board_code").reset_index(drop=True)

    def find_board(
        self,
        key: str,
        as_of: str | date | None = None,
    ) -> dict[str, Any] | None:
        frame = self.list_boards(as_of=as_of)
        if frame.empty:
            return None
        text = str(key).strip()
        matched = frame[
            (frame["board_code"].astype(str) == text)
            | (frame["name"].astype(str) == text)
        ]
        if matched.empty:
            matched = frame[frame["name"].astype(str).str.contains(text, na=False)]
        if matched.empty:
            return None
        return matched.iloc[0].to_dict()

    def members(
        self,
        board_code: str,
        as_of: str | date | None = None,
        *,
        source: str | None = None,
    ) -> pd.DataFrame:
        snapshot = self.member_snapshot_ts(board_code, as_of)
        if snapshot is None:
            return pd.DataFrame(columns=MEMBER_COLUMNS)
        sql = f"SELECT * FROM {_MEMBER_TABLE} WHERE board_code = %s AND ts = %s"
        params: list[Any] = [board_code, snapshot]
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY stock_code"
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=MEMBER_COLUMNS)
        frame = _with_ts(frame)
        # 同一快照日可能有多源：按 stock_code 去重，保留行数最多的源
        if "source" in frame.columns and frame["source"].nunique() > 1:
            counts = frame.groupby("source").size()
            keep = counts.idxmax()
            frame = frame[frame["source"] == keep]
        return frame.reset_index(drop=True)

    def member_snapshot_ts(
        self, board_code: str, as_of: str | date | None = None
    ) -> datetime | None:
        sql = f"SELECT max(ts) FROM {_MEMBER_TABLE} WHERE board_code = %s"
        params: list[Any] = [board_code]
        if as_of is not None:
            sql += " AND ts <= %s"
            params.append(_day(as_of))
        row = self.db.query(sql, params, fetch_all=False)
        if not row or row[0] is None:
            return None
        return pd.Timestamp(row[0]).to_pydatetime()

    def member_count(self, board_code: str, as_of: str | date | None = None) -> int:
        snapshot = self.member_snapshot_ts(board_code, as_of)
        if snapshot is None:
            return 0
        row = self.db.query(
            f"SELECT count() FROM {_MEMBER_TABLE} WHERE board_code = %s AND ts = %s",
            [board_code, snapshot],
            fetch_all=False,
        )
        return int(row[0]) if row and row[0] is not None else 0

    def daily(
        self,
        board_codes: Sequence[str],
        start: str | date,
        end: str | date,
        *,
        source: str | None = None,
    ) -> pd.DataFrame:
        codes = [str(code) for code in board_codes if str(code)]
        if not codes:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        placeholders = ",".join(["%s"] * len(codes))
        sql = (
            f"SELECT * FROM {_DAILY_TABLE} "
            f"WHERE board_code IN ({placeholders}) "
            "AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [
            *codes,
            _day(start),
            datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0),
        ]
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY ts, board_code"
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        frame = _with_ts(frame)
        return frame.reset_index(drop=True)

    def classification(self, as_of: str | date | None = None) -> pd.DataFrame:
        if as_of is None:
            sql = f"SELECT * FROM {_CLASS_TABLE} LATEST ON ts PARTITION BY concept_code"
            params: list[Any] = []
        else:
            sql = (
                f"SELECT * FROM {_CLASS_TABLE} "
                "WHERE ts = (SELECT max(ts) FROM "
                f"{_CLASS_TABLE} WHERE ts <= %s)"
            )
            params = [_day(as_of)]
        sql += " ORDER BY concept_code"
        frame = self.db.query_dataframe(sql, params)
        frame = _with_ts(frame)
        return frame.reset_index(drop=True) if frame is not None else frame
