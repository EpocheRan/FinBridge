from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from screener.domain.baseline import BaselineConfig, build_profile
from screener.domain.build_state import (
    BuildQuality,
    BuildState,
    BuildStatus,
)
from screener.domain.coverage import classify_gaps
from screener.domain.errors import InvalidScreenSpec
from screener.domain.factor_engine import DailyFactorConfig, FactorEngine
from screener.domain.models import GapType, ScreenSpec


def _daily_frames(days: int = 300):
    dates = pd.bdate_range("2024-01-01", periods=days)
    close = pd.Series(np.linspace(10.0, 30.0, days), index=dates)
    raw = pd.DataFrame(
        {
            "ts": dates,
            "code": "600519",
            "open": close.to_numpy() * 0.99,
            "high": close.to_numpy() * 1.01,
            "low": close.to_numpy() * 0.98,
            "close": close.to_numpy(),
            "volume": 1_000_000,
            "amount": 10_000_000,
            "turnover": 1.0,
        }
    )
    benchmark = pd.DataFrame({"ts": dates, "close": close.to_numpy()})
    reference = pd.DataFrame(
        [{"code": "600519", "total_share": 1e9, "float_share": 1e9}]
    )
    return raw, benchmark, reference


def test_factor_engine_produces_expected_columns_and_flags():
    raw, benchmark, reference = _daily_frames()
    engine = FactorEngine(DailyFactorConfig())
    frame = engine.compute_daily(raw, benchmark, reference)

    assert len(frame) == len(raw)
    assert frame["lookback_days"].iloc[-1] == len(raw)
    assert frame["full_ready"].iloc[-1]
    assert not frame["is_partial"].iloc[-1]
    assert frame["atr14"].iloc[-1] > 0
    assert frame["high_52w"].iloc[-1] >= frame["close"].iloc[-1] * 0.98
    assert frame["beta_252"].iloc[-1] == pytest.approx(1.0, rel=1e-4)
    assert frame["pct_chg"].iloc[-1] > 0


def test_factor_engine_marks_short_history_partial():
    raw, benchmark, reference = _daily_frames(days=30)
    engine = FactorEngine(DailyFactorConfig())
    frame = engine.compute_daily(raw, benchmark, reference)
    assert frame["is_partial"].iloc[-1]
    assert not frame["full_ready"].iloc[-1]
    assert pd.isna(frame["beta_252"].iloc[-1])


def test_baseline_profile_cumulative_volume():
    rows = []
    for day in pd.bdate_range("2026-09-01", periods=10):
        for minute in list(range(575, 691, 5)) + list(range(785, 901, 5)):
            rows.append(
                {
                    "ts": day + pd.Timedelta(minutes=minute),
                    "code": "600519",
                    "volume": 1000,
                    "amount": 10000,
                }
            )
    profile = build_profile(
        pd.DataFrame(rows),
        BaselineConfig(lookback_days=10, min_sample_days=5),
    )
    assert len(profile) == 48
    assert profile["sample_days"].iloc[0] == 10
    assert profile["avg_cum_volume"].iloc[0] == 1000
    assert profile["avg_cum_volume"].iloc[1] == 2000
    assert bool(profile["includes_auction"].iloc[0])
    assert not bool(profile["is_partial"].iloc[0])


def test_classify_gaps_priority():
    expected = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)]
    gaps = classify_gaps(
        code="600519",
        expected_dates=expected,
        factor_dates={date(2026, 10, 20)},
        raw_dates={date(2026, 10, 20), date(2026, 10, 21)},
        suspended_dates={date(2026, 10, 22)},
    )
    types = {gap.gap_type for gap in gaps}
    assert GapType.FACTOR_MISSING in types
    assert GapType.SUSPENDED in types


def test_build_state_claim_partial_and_retry_gate():
    state = BuildState.new_snapshot(
        dataset="screener_daily",
        as_of=date(2026, 10, 23),
        universe_key="ALL",
        universe_payload="",
        freq="d",
        formula_version="daily-v1",
        requested_codes=10,
    )
    assert state.status == BuildStatus.PENDING
    assert state.claim("owner", 60)
    assert state.status == BuildStatus.RUNNING
    state.mark_partial(
        "missing",
        coverage_ratio=0.5,
        retry_at=datetime.now() + timedelta(hours=1),
    )
    assert state.status == BuildStatus.PARTIAL
    assert state.quality == BuildQuality.PARTIAL
    assert not state.can_resume(datetime.now())
    future = datetime.now() + timedelta(days=2)
    assert state.can_resume(future)


def test_screen_spec_rejects_unknown_field():
    with pytest.raises(InvalidScreenSpec):
        ScreenSpec.from_dict(
            {"as_of": "2026-10-23", "filters": [{"field": "drop", "op": "eq", "value": 1}]}
        )


def test_screen_spec_allows_derived_field():
    spec = ScreenSpec.from_dict(
        {
            "as_of": "2026-10-23",
            "filters": [
                {"field": "price_x_avg_volume", "op": "gt", "value": 1_000_000}
            ],
            "sort": [{"field": "pct_chg", "desc": True}],
        }
    )
    assert spec.filters[0].field == "price_x_avg_volume"
    assert spec.sort[0].field == "pct_chg"
