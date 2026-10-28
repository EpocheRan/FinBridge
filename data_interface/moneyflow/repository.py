"""分档数据仓储（QuestDB）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

from .models import CANONICAL_COLUMNS, RANK_FIELDS

logger = logging.getLogger("DataInterface.Moneyflow.Repository")

_TABLE = '"moneyflow_daily"'
_SOURCE_TABLE = '"moneyflow_source"'


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


def _day_bounds(start: str | date, end: str | date) -> tuple[datetime, datetime]:
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    return (
        datetime.combine(start_ts.date(), time.min),
        datetime.combine(end_ts.date(), time.max).replace(microsecond=0),
    )


class MoneyflowRepository:
    def __init__(self, db: QuestDBClient | None = None, config: dict[str, Any] | None = None):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)

    # ------------------------------------------------------------------ 写入
    def upsert(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        missing = [column for column in CANONICAL_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"分档数据缺少列: {missing}")
        rows = [
            tuple(_clean(row[column]) for column in CANONICAL_COLUMNS)
            for _, row in frame.iterrows()
        ]
        sql = (
            f"INSERT INTO {_TABLE} ({', '.join(CANONICAL_COLUMNS)}) VALUES %s"
        )
        return self.db.execute_values(sql, rows, page_size=1000)

    def upsert_source_meta(self, payload: dict[str, Any]) -> int:
        columns = [
            "updated_at",
            "source",
            "vendor",
            "dataset",
            "unit",
            "definition",
            "coverage_start",
            "coverage_end",
            "priority",
            "enabled",
            "notes",
        ]
        row = {
            "updated_at": payload.get("updated_at") or now_cn(),
            "source": payload["source"],
            "vendor": payload.get("vendor"),
            "dataset": payload.get("dataset"),
            "unit": payload.get("unit"),
            "definition": payload.get("definition"),
            "coverage_start": _clean(payload.get("coverage_start")),
            "coverage_end": _clean(payload.get("coverage_end")),
            "priority": int(payload.get("priority", 100)),
            "enabled": bool(payload.get("enabled", True)),
            "notes": payload.get("notes"),
        }
        sql = f"INSERT INTO {_SOURCE_TABLE} ({', '.join(columns)}) VALUES %s"
        return self.db.execute_values(sql, [tuple(row[column] for column in columns)])

    # ------------------------------------------------------------------ 查询
    def query_by_code(
        self,
        code: str,
        start: str | date,
        end: str | date,
        source: str | None = None,
    ) -> pd.DataFrame:
        normalized = normalize_code(code)
        if not normalized:
            raise ValueError(f"无效代码: {code!r}")
        start_ts, end_ts = _day_bounds(start, end)
        sql = f"SELECT * FROM {_TABLE} WHERE code = %s AND ts BETWEEN %s AND %s"
        params: list[Any] = [normalized, start_ts, end_ts]
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY ts"
        return self.db.query_dataframe(sql, params)

    def query_by_date(self, trade_date: str | date, source: str | None = None) -> pd.DataFrame:
        start_ts, _ = _day_bounds(trade_date, trade_date)
        sql = f"SELECT * FROM {_TABLE} WHERE ts = %s"
        params: list[Any] = [start_ts]
        if source:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY code"
        return self.db.query_dataframe(sql, params)

    def rank(
        self,
        trade_date: str | date,
        field: str = "main_amount",
        *,
        source: str,
        limit: int = 50,
        ascending: bool = False,
    ) -> pd.DataFrame:
        expression = RANK_FIELDS.get(field)
        if not expression:
            raise ValueError(
                f"未知排序字段: {field!r}，可选: {sorted(RANK_FIELDS)}"
            )
        start_ts, _ = _day_bounds(trade_date, trade_date)
        direction = "ASC" if ascending else "DESC"
        sql = (
            f"SELECT *, {expression} AS rank_value FROM {_TABLE} "
            "WHERE ts = %s AND source = %s "
            f"ORDER BY rank_value {direction} LIMIT %s"
        )
        return self.db.query_dataframe(sql, [start_ts, source, int(limit)])

    def latest_ts(self, source: str | None = None) -> pd.Timestamp | None:
        sql = f"SELECT max(ts) AS latest FROM {_TABLE}"
        params: list[Any] = []
        if source:
            sql += " WHERE source = %s"
            params.append(source)
        row = self.db.query(sql, params, fetch_all=False)
        if not row or row[0] is None:
            return None
        return pd.Timestamp(row[0])

    def count_for_date(self, trade_date: str | date, source: str | None = None) -> int:
        start_ts, _ = _day_bounds(trade_date, trade_date)
        sql = f"SELECT count() FROM {_TABLE} WHERE ts = %s"
        params: list[Any] = [start_ts]
        if source:
            sql += " AND source = %s"
            params.append(source)
        row = self.db.query(sql, params, fetch_all=False)
        return int(row[0]) if row and row[0] is not None else 0

    def sources(self, *, only_enabled: bool = False) -> pd.DataFrame:
        sql = f"SELECT * FROM {_SOURCE_TABLE}"
        if only_enabled:
            sql += " WHERE enabled = true"
        sql += " ORDER BY priority"
        return self.db.query_dataframe(sql)

    def resolve_source(self, preferred: Sequence[str] | None = None) -> str:
        """按配置优先级挑一个可用源；都没有则返回首选源 ID。"""
        priority = list(preferred or [])
        if not priority:
            priority = list(
                (self.config.get("moneyflow", {}) or {}).get("source_priority", [])
            )
        if not priority:
            priority = ["tushare.moneyflow"]
        try:
            available = set(self.sources(only_enabled=True)["source"].astype(str))
        except Exception:
            available = set()
        for source in priority:
            if not available or source in available:
                return source
        return priority[0]
