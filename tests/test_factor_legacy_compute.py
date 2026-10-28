"""23 个 legacy 因子的 compute 测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factor_engine.definitions.legacy import LEGACY_COLUMNS, LegacyComputer
from factor_engine.service import FactorEngineService
from factors.spec import FactorSpec


def make_raw(code="AAA", days=320):
    dates = pd.bdate_range("2024-01-02", periods=days)
    close = 10 + np.arange(days) * 0.01
    return pd.DataFrame(
        {
            "ts": dates,
            "code": code,
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 10000 + np.arange(days),
            "amount": (10000 + np.arange(days)) * close,
            "turnover": 1.2,
            "adj_factor": 1.0,
        }
    )


class FakeLegacyData:
    def __init__(self, raw):
        self.raw = raw
        self.bench = pd.DataFrame(
            {"ts": raw["ts"], "close": 100 + np.arange(len(raw)) * 0.05}
        )
        self.ref = pd.DataFrame(
            [
                {
                    "code": "AAA",
                    "total_share": 1_000_000_000,
                    "float_share": 800_000_000,
                    "total_mv": 1e10,
                    "float_mv": 8e9,
                }
            ]
        )

    def load(self, codes, start, end):
        return self.raw

    def load_benchmark(self, code="000300", start=None, end=None):
        return self.bench

    def load_reference(self, codes):
        return self.ref


def test_legacy_computer_outputs_23_columns():
    raw = make_raw()
    computer = LegacyComputer(FakeLegacyData(raw))
    wide = computer.compute(raw, ["AAA"], raw["ts"].min(), raw["ts"].max())
    assert len(wide) == len(raw)
    for column in (
        "pct_chg",
        "atr_pct",
        "volatility_20d",
        "close_vs_ma250_pct",
        "dist_high_52w_pct",
        "beta_252",
        "total_mv",
        "float_share",
        "is_new_high_52w",
        "lookback_days",
    ):
        assert column in wide.columns
    assert wide["pct_chg"].notna().sum() > 0
    assert wide["total_mv"].dropna().iloc[-1] > 0
    for column in LEGACY_COLUMNS.values():
        assert wide[column].notna().any(), f"{column} 全为空"


def test_legacy_service_build_writes_values():
    raw = make_raw()
    data = FakeLegacyData(raw)

    class FakeStore:
        def __init__(self):
            self.values = []
            self.latest = []
            self.states = []

        def upsert_values(self, frame):
            self.values.append(frame.copy())
            return len(frame)

        def upsert_latest(self, frame):
            self.latest.append(frame.copy())
            return len(frame)

        def upsert_build_state(self, payload):
            self.states.append(payload)
            return 1

        def latest_ts(self, factor_id):
            return pd.Timestamp(raw["ts"].max())

    store = FakeStore()
    service = FactorEngineService(store=store, data=data, config={})
    result = service.build(
        ["technical.pct_chg", "technical.total_mv"],
        "2024-12-01",
        raw["ts"].max(),
        ["AAA"],
    )
    assert result["rows_written"] > 0
    frame = store.values[0]
    assert set(frame["factor_id"]) == {"technical.pct_chg", "technical.total_mv"}
    assert frame["value"].notna().all()
    assert store.states[0]["status"] == "DONE"


def test_all_legacy_ids_registered_once():
    service = FactorEngineService(config={})
    ids = [spec.factor_id for spec in service.list_factors()]
    assert len(ids) == 43
    assert len(set(ids)) == 43
    for factor_id in LEGACY_COLUMNS:
        assert factor_id in ids
