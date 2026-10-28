"""factor_daily_wide 写路径归属 factor_engine。"""

from __future__ import annotations

import pandas as pd

from factor_engine.wide_store import WIDE_DB_COLUMNS, upsert_wide


class FakeDB:
    def __init__(self):
        self.calls = []

    def execute_values(self, sql, rows, page_size=500):
        self.calls.append((sql, rows, page_size))
        return len(rows)


def test_upsert_wide_writes_factor_daily_wide():
    frame = pd.DataFrame(
        [
            {
                "ts": pd.Timestamp("2026-10-27"),
                "code": "600519",
                "market": "MLCN",
                "close": 1355.9,
                "volume": 3866914,
                "is_partial": False,
                "formula_version": "daily-v1",
            }
        ]
    )
    db = FakeDB()
    written = upsert_wide(db, frame)
    assert written == 1
    sql, rows, page_size = db.calls[0]
    assert 'INSERT INTO "factor_daily_wide"' in sql
    assert len(rows[0]) == len(WIDE_DB_COLUMNS)
    assert page_size == 500


def test_upsert_wide_empty_returns_zero():
    db = FakeDB()
    assert upsert_wide(db, pd.DataFrame()) == 0
    assert db.calls == []
