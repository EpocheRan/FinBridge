"""factor_engine M2：内置因子、计算、ensure、钩子、对账。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from data_interface.common.events import DataWritten
from factor_engine.audit import compare_frames
from factor_engine.definitions import builtin_map
from factor_engine.hooks import FactorHooks
from factor_engine.service import FactorEngineService
from factors.spec import Tolerance


def price_frame(code="600519", periods=40):
    dates = pd.bdate_range("2026-01-01", periods=periods)
    close = 10 + np.arange(periods) * 0.1
    return pd.DataFrame(
        {
            "ts": dates,
            "code": code,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000,
            "amount": 10000.0,
            "turnover": 1.0,
            "adj_factor": 1.0,
            "hfq_close": close,
            "hfq_high": close + 0.2,
            "hfq_low": close - 0.2,
            "hfq_open": close,
        }
    )


class FakeData:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def load(self, codes, start, end):
        self.calls.append((codes, str(start), str(end)))
        return self.frame.copy()


class FakeStore:
    def __init__(self):
        self.values = []
        self.latest = []
        self.states = []
        self._latest_ts = None

    def upsert_values(self, frame):
        self.values.append(frame.copy())
        self._latest_ts = pd.to_datetime(frame["ts"]).max()
        return len(frame)

    def upsert_latest(self, frame):
        self.latest.append(frame.copy())
        return len(frame)

    def upsert_build_state(self, payload):
        self.states.append(payload)
        return 1

    def latest_ts(self, factor_id):
        return self._latest_ts

    def query_values(self, *args, **kwargs):
        return pd.DataFrame()

    def query_latest(self, *args, **kwargs):
        return pd.DataFrame()


def make_service(frame=None):
    store = FakeStore()
    data = FakeData(frame or price_frame())
    return FactorEngineService(store=store, data=data, config={}), store, data


def test_builtin_definitions_have_three_tags():
    mapping = builtin_map()
    assert "momentum.ret" in mapping
    assert "technical.atr" in mapping
    spec = mapping["momentum.ret"].spec
    assert spec.category.value == "momentum"
    assert spec.source.dataset == "ohlcv_1d"
    assert spec.window == 20 and spec.freq == "1d"


def test_build_writes_values_and_latest():
    service, store, data = make_service()
    result = service.build(["momentum.ret"], "2026-01-01", "2026-03-01", ["600519"])

    assert result["rows_written"] > 0
    assert store.values and store.latest
    frame = store.values[0]
    assert set(frame["factor_id"]) == {"momentum.ret"}
    assert (frame["source"] == "ohlcv_1d").all()
    assert frame["version"].iloc[0] == "v1"
    assert frame["source_version"].iloc[0].startswith("ohlcv_1d:")
    assert data.calls[0][0] == ["600519"]
    assert store.states[0]["status"] == "DONE"


def test_build_skips_warmup_nan():
    service, store, _ = make_service()
    result = service.build(["technical.atr"], "2026-01-01", "2026-03-01", ["600519"])
    assert result["rows_written"] == 40 - 14 + 1
    assert store.values[0]["value"].notna().all()


def test_ensure_builds_when_missing_then_skips():
    service, store, _ = make_service()
    first = service.ensure(["600519"], "2026-02-20")
    assert first["built"] is True
    second = service.ensure(["600519"], "2026-02-20")
    assert second["built"] is False


def test_hooks_dispatch_ohlcv_event():
    class FakeService:
        def __init__(self):
            self.calls = []
            self.store = FakeStore()

        def build(self, factor_ids, start, end, codes=None):
            self.calls.append((tuple(factor_ids), start, end, codes))
            return {"rows_written": 3}

    service = FakeService()
    hooks = FactorHooks(service)
    event = DataWritten(
        dataset="ohlcv_1d",
        dates=(date(2026, 3, 2), date(2026, 3, 1)),
        codes=("600519",),
    )
    result = hooks.on_data_written(event)
    assert result["rows_written"] == 3
    assert service.calls[0][0] == (
        "momentum.ret",
        "reversal.short",
        "technical.atr",
        "risk.vol",
        "liquidity.turnover",
    )
    assert str(service.calls[0][1]) == "2026-03-01"
    assert str(service.calls[0][2]) == "2026-03-02"

    assert hooks.on_data_written(DataWritten(dataset="unknown")) is None


def test_audit_uses_relative_tolerance_for_large_values():
    left = pd.DataFrame(
        [
            {"ts": "2026-03-01", "code": "600519", "factor_id": "size.log_mv", "value": 1e12},
            {"ts": "2026-03-02", "code": "600519", "factor_id": "size.log_mv", "value": 1.0},
        ]
    )
    right = left.copy()
    right.loc[0, "value"] = 1e12 + 100  # 相对 1e-10，应通过
    right.loc[1, "value"] = 1.001  # 相对 1e-3，应失败
    result = compare_frames(left, right, tolerance=Tolerance())
    assert result["checked"] == 2
    assert result["failed"] == 1
    assert result["max_rel_diff"] > 1e-6
