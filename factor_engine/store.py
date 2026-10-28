"""因子表仓储（factor_engine 唯一写者）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Iterable, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

logger = logging.getLogger("FactorEngine.Store")

REGISTRY_TABLE = '"factor_registry"'
ALIAS_TABLE = '"factor_alias"'
DAILY_TABLE = '"factor_value_daily"'
ANNOUNCE_TABLE = '"factor_value_announce"'
LATEST_TABLE = '"factor_value_latest"'
BUILD_TABLE = '"factor_build_state"'
AUDIT_TABLE = '"factor_audit_state"'

VALUE_COLUMNS = [
    "ts",
    "code",
    "factor_id",
    "value",
    "quality",
    "source",
    "source_version",
    "version",
    "computed_at",
]
ANNOUNCE_COLUMNS = [
    "ts",
    "code",
    "factor_id",
    "report_period",
    "announce_date",
    "lag_days",
    "value",
    "quality",
    "source",
    "source_version",
    "version",
    "computed_at",
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
        raise ValueError(f"因子数据缺少列: {missing}")
    return [
        tuple(_clean(row[column]) for column in columns) for _, row in frame.iterrows()
    ]


def _day(value: str | date | datetime) -> datetime:
    return datetime.combine(pd.Timestamp(value).date(), time.min)


class FactorStore:
    def __init__(self, db: QuestDBClient | None = None, config: dict | None = None):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config, connect=False)

    # ------------------------------------------------------------------ registry
    def upsert_registry(self, specs: Iterable[Any]) -> int:
        rows: list[tuple] = []
        now = pd.Timestamp.now().to_pydatetime()
        for spec in specs:
            rows.append(
                (
                    now,
                    spec.factor_id,
                    spec.name,
                    spec.category.value,
                    spec.subcategory,
                    spec.source.dataset,
                    spec.source.vendor,
                    ",".join(spec.source.fields),
                    spec.freq,
                    int(spec.window),
                    int(spec.lag),
                    spec.value_kind,
                    spec.update_mode,
                    str(spec.unit),
                    spec.direction,
                    spec.price_basis,
                    spec.version,
                    spec.source_version_policy,
                    ";".join(dep.token for dep in spec.dependencies),
                    int(spec.warmup),
                    spec.formula,
                    bool(spec.enabled),
                    str(spec.materialization.value),
                )
            )
        sql = (
            f"INSERT INTO {REGISTRY_TABLE} "
            "(updated_at, factor_id, name, category, subcategory, source_dataset, source_vendor, "
            "source_fields, freq, window, lag, value_kind, update_mode, unit, direction, "
            "price_basis, version, source_version_policy, dependencies, warmup, formula, enabled, "
            "materialization) "
            "VALUES %s"
        )
        return self.db.execute_values(sql, rows)

    def upsert_aliases(self, aliases: Iterable[tuple[str, str, int | None]]) -> int:
        rows = [
            (
                pd.Timestamp.now().to_pydatetime(),
                alias,
                factor_id,
                None if window is None else int(window),
                "{}",
                None,
            )
            for alias, factor_id, window in aliases
        ]
        sql = (
            f"INSERT INTO {ALIAS_TABLE} (updated_at, alias, factor_id, window, params, notes) "
            "VALUES %s"
        )
        return self.db.execute_values(sql, rows)

    # ------------------------------------------------------------------ values
    def upsert_values(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {DAILY_TABLE} ({', '.join(VALUE_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, VALUE_COLUMNS), page_size=1000)

    def upsert_latest(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {LATEST_TABLE} ({', '.join(VALUE_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, VALUE_COLUMNS), page_size=1000)

    def upsert_announce(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        sql = f"INSERT INTO {ANNOUNCE_TABLE} ({', '.join(ANNOUNCE_COLUMNS)}) VALUES %s"
        return self.db.execute_values(sql, _rows(frame, ANNOUNCE_COLUMNS), page_size=1000)

    # ------------------------------------------------------------------ state
    def upsert_build_state(self, payload: dict[str, Any]) -> int:
        columns = [
            "updated_at",
            "job_id",
            "dataset",
            "factor_ids",
            "window_start",
            "window_end",
            "cursor",
            "status",
            "attempts",
            "retry_at",
            "rows_written",
            "last_error",
            "payload",
        ]
        row = dict(payload)
        row.setdefault("updated_at", pd.Timestamp.now().to_pydatetime())
        row.setdefault("attempts", 0)
        sql = f"INSERT INTO {BUILD_TABLE} ({', '.join(columns)}) VALUES %s"
        return self.db.execute_values(sql, [tuple(_clean(row.get(c)) for c in columns)])

    def upsert_audit(self, payload: dict[str, Any]) -> int:
        columns = [
            "updated_at",
            "audit_id",
            "dataset",
            "factor_ids",
            "window_start",
            "window_end",
            "status",
            "checked",
            "failed",
            "max_abs_diff",
            "max_rel_diff",
            "details",
            "payload",
        ]
        row = dict(payload)
        row.setdefault("updated_at", pd.Timestamp.now().to_pydatetime())
        sql = f"INSERT INTO {AUDIT_TABLE} ({', '.join(columns)}) VALUES %s"
        return self.db.execute_values(sql, [tuple(_clean(row.get(c)) for c in columns)])

    # ------------------------------------------------------------------ query
    def query_values(
        self,
        factor_ids: Sequence[str],
        start: str | date,
        end: str | date,
        codes: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        ids = [str(item) for item in factor_ids]
        if not ids:
            return pd.DataFrame(columns=VALUE_COLUMNS)
        placeholders = ",".join(["%s"] * len(ids))
        sql = (
            f"SELECT * FROM {DAILY_TABLE} "
            f"WHERE factor_id IN ({placeholders}) AND ts BETWEEN %s AND %s"
        )
        params: list[Any] = [
            *ids,
            _day(start),
            datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0),
        ]
        if codes:
            code_placeholders = ",".join(["%s"] * len(codes))
            sql += f" AND code IN ({code_placeholders})"
            params.extend(str(code) for code in codes)
        sql += " ORDER BY ts, code, factor_id"
        frame = self.db.query_dataframe(sql, params)
        return frame.reset_index() if frame is not None and not frame.empty else frame

    def query_latest(
        self, factor_ids: Sequence[str], codes: Sequence[str] | None = None
    ) -> pd.DataFrame:
        ids = [str(item) for item in factor_ids]
        if not ids:
            return pd.DataFrame(columns=VALUE_COLUMNS)
        placeholders = ",".join(["%s"] * len(ids))
        sql = (
            f"SELECT * FROM {LATEST_TABLE} WHERE factor_id IN ({placeholders})"
        )
        params: list[Any] = list(ids)
        if codes:
            code_placeholders = ",".join(["%s"] * len(codes))
            sql += f" AND code IN ({code_placeholders})"
            params.extend(str(code) for code in codes)
        sql += " LATEST ON ts PARTITION BY code, factor_id"
        frame = self.db.query_dataframe(sql, params)
        return frame.reset_index() if frame is not None and not frame.empty else frame

    def latest_ts(self, factor_id: str) -> pd.Timestamp | None:
        row = self.db.query(
            f"SELECT max(ts) FROM {DAILY_TABLE} WHERE factor_id = %s",
            [str(factor_id)],
            fetch_all=False,
        )
        if not row or row[0] is None:
            return None
        return pd.Timestamp(row[0])
