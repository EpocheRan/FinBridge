"""factor_daily_wide 写路径（factor_engine 拥有）。

screener 只负责编排/查询；宽表写入实现放在这里，避免 screener 持有写逻辑。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .wide import DAILY_COLUMNS

WIDE_DB_COLUMNS = [
    "ts",
    "code",
    "market",
    *[column for column in DAILY_COLUMNS if column not in ("ts", "code")],
    "formula_version",
    "computed_at",
]

_BOOL_COLUMNS = {
    "is_new_high_52w",
    "is_new_low_52w",
    "is_partial",
    "core_ready",
    "full_ready",
}
_INT_COLUMNS = {"volume", "beta_window", "lookback_days"}


def _clean(value: Any) -> Any:
    if value is None:
        return None
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


def daily_to_rows(frame: pd.DataFrame) -> list[tuple]:
    if frame is None or frame.empty:
        return []
    data = frame.copy()
    for column in WIDE_DB_COLUMNS:
        if column not in data.columns:
            data[column] = None
    rows: list[tuple] = []
    for _, row in data.iterrows():
        values: list[Any] = []
        for column in WIDE_DB_COLUMNS:
            value = _clean(row[column])
            if column in _INT_COLUMNS and value is not None:
                value = int(value)
            elif column in _BOOL_COLUMNS and value is not None:
                value = bool(value)
            values.append(value)
        rows.append(tuple(values))
    return rows


def upsert_wide(db, frame: pd.DataFrame) -> int:
    rows = daily_to_rows(frame)
    if not rows:
        return 0
    columns = ", ".join(f'"{column}"' for column in WIDE_DB_COLUMNS)
    sql = f'INSERT INTO "factor_daily_wide" ({columns}) VALUES %s'
    return db.execute_values(sql, rows, page_size=500)
