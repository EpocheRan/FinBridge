"""概念板块仓储（QuestDB）。"""

from __future__ import annotations

import logging
import time
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
_DIM_BOARD_TABLE = '"dim_board"'
_MAP_BOARD_TABLE = '"map_stock_board"'

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

#: ``dim_board``：板块维度（东财 BK 命名空间与同花顺 3000xx **物理隔离**，靠 source 区分）
DIM_BOARD_COLUMNS = [
    "ts",
    "board_id",
    "board_type",
    "board_name",
    "parent_id",
    "source",
]
#: ``map_stock_board``：个股↔板块 PIT 映射
MAP_BOARD_COLUMNS = [
    "ts",
    "code",
    "board_id",
    "board_type",
    "board_name",
    "is_primary",
    "effective_from",
    "effective_to",
    "source",
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

    def upsert_dim_board(self, frame: pd.DataFrame) -> int:
        """写 ``dim_board``（东财 BK 板块维度）。"""
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_DIM_BOARD_TABLE} ({', '.join(DIM_BOARD_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, DIM_BOARD_COLUMNS), page_size=500)

    def upsert_map_stock_board(self, frame: pd.DataFrame) -> int:
        """写 ``map_stock_board``（个股↔板块 PIT 映射）。"""
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {_MAP_BOARD_TABLE} ({', '.join(MAP_BOARD_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, MAP_BOARD_COLUMNS), page_size=500)

    def stock_boards(
        self, code: str, as_of: str | date | None = None, *, source: str | None = None
    ) -> pd.DataFrame:
        """某只股票在 ``as_of`` 之前最近一次快照里的全部板块归属。"""
        sql = (
            f"SELECT * FROM {_MAP_BOARD_TABLE} WHERE code = %s "
            "AND ts = (SELECT max(ts) FROM "
            f"{_MAP_BOARD_TABLE} WHERE code = %s"
        )
        params: list[Any] = [str(code), str(code)]
        if as_of is not None:
            sql += " AND ts <= %s"
            params.append(_day(as_of))
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += ") ORDER BY board_type, board_name"
        frame = None
        # QuestDB WAL 异步可见：刚写完立刻查可能为空，短暂重试
        for attempt in range(3):
            try:
                frame = self.db.query_dataframe(sql, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("个股板块归属查询失败 %s: %s", code, exc)
                return pd.DataFrame(columns=MAP_BOARD_COLUMNS)
            if frame is not None and not frame.empty:
                break
            time.sleep(0.3 * (attempt + 1))
        if frame is None or frame.empty:
            return pd.DataFrame(columns=MAP_BOARD_COLUMNS)
        return _with_ts(frame).reset_index(drop=True)

    def board_members(self, board_id: str, as_of: str | date | None = None) -> list[str]:
        """某板块在 ``as_of`` 之前最近一次快照里的成分股代码。"""
        sql = (
            f"SELECT code FROM {_MAP_BOARD_TABLE} WHERE board_id = %s "
            "AND ts = (SELECT max(ts) FROM "
            f"{_MAP_BOARD_TABLE} WHERE board_id = %s"
        )
        params: list[Any] = [str(board_id), str(board_id)]
        if as_of is not None:
            sql += " AND ts <= %s"
            params.append(_day(as_of))
        sql += ") ORDER BY code"
        try:
            rows = self.db.query(sql, params) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("板块成分查询失败 %s: %s", board_id, exc)
            return []
        return [str(row[0]) for row in rows if row and row[0]]

    def dim_boards(self, board_type: str | None = None) -> pd.DataFrame:
        sql = f"SELECT * FROM {_DIM_BOARD_TABLE} LATEST ON ts PARTITION BY board_id, board_type, source"
        params: list[Any] = []
        if board_type:
            sql += " WHERE board_type = %s"
            params.append(board_type)
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("板块维度查询失败: %s", exc)
            return pd.DataFrame(columns=DIM_BOARD_COLUMNS)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=DIM_BOARD_COLUMNS)
        return _with_ts(frame).reset_index(drop=True)

    # ------------------------------------------------------- 自算板块收益用
    def daily_prices(
        self, codes: Sequence[str], start: str | date, end: str | date, *, chunk: int = 200
    ) -> pd.DataFrame:
        """从 ``ohlcv_1d`` 取收盘价（不复权价 + adj_factor 由调用方决定是否复权）。"""
        codes = [str(code) for code in codes if str(code)]
        if not codes:
            return pd.DataFrame(columns=["ts", "code", "close"])
        frames: list[pd.DataFrame] = []
        for offset in range(0, len(codes), chunk):
            batch = codes[offset : offset + chunk]
            placeholders = ",".join(["%s"] * len(batch))
            sql = (
                'SELECT ts, code, close FROM "ohlcv_1d" '
                f"WHERE code IN ({placeholders}) AND ts BETWEEN %s AND %s AND type <> 'idx'"
            )
            params: list[Any] = [
                *batch,
                _day(start),
                datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0),
            ]
            try:
                frame = self.db.query_dataframe(sql, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("日线批量查询失败(chunk=%s): %s", offset, exc)
                continue
            if frame is not None and not frame.empty:
                frames.append(_with_ts(frame))
        if not frames:
            return pd.DataFrame(columns=["ts", "code", "close"])
        return pd.concat(frames, ignore_index=True)

    def float_market_caps(
        self, codes: Sequence[str], start: str | date, end: str | date, *, chunk: int = 200
    ) -> pd.DataFrame:
        """从 ``fin_valuation_daily`` 取流通市值（用于市值加权板块收益）。"""
        codes = [str(code) for code in codes if str(code)]
        if not codes:
            return pd.DataFrame(columns=["ts", "code", "float_market_cap"])
        frames: list[pd.DataFrame] = []
        for offset in range(0, len(codes), chunk):
            batch = codes[offset : offset + chunk]
            placeholders = ",".join(["%s"] * len(batch))
            sql = (
                'SELECT ts, code, float_market_cap FROM "fin_valuation_daily" '
                f"WHERE code IN ({placeholders}) AND ts BETWEEN %s AND %s AND type <> 'idx'"
            )
            params: list[Any] = [
                *batch,
                _day(start),
                datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0),
            ]
            try:
                frame = self.db.query_dataframe(sql, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("流通市值批量查询失败(chunk=%s): %s", offset, exc)
                continue
            if frame is not None and not frame.empty:
                frames.append(_with_ts(frame))
        if not frames:
            return pd.DataFrame(columns=["ts", "code", "float_market_cap"])
        return pd.concat(frames, ignore_index=True)

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
