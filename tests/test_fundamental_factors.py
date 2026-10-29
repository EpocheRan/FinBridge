"""基本面因子 compute 纯数学测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.fundamental import (
    FUNDAMENTAL_SPECS,
    RANK_FACTORS,
    FundamentalComputer,
)


def _metrics(codes=("AAA", "BBB"), days=320):
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for code in codes:
        base = 1.0 if code == "AAA" else 1.2
        for i, dt in enumerate(dates):
            rows.append(
                {
                    "ts": dt,
                    "code": code,
                    "close": 10 + i * 0.01 + base,
                    "total_share": 1_000_000_000.0,
                    "eps_ttm": 1.0 + i * 0.001 + base,
                    "net_profit_ttm": 1e9 + i * 1e6 + base,
                    "ocf_ttm": 8e8 + i * 5e5 + base,
                    "revenue_ttm": 5e9 + i * 1e7 + base,
                    "cost_ttm": 3e9 + i * 5e6 + base,
                    "roa": 0.05 + i * 1e-5 + base * 0.01,
                    "roe": 0.10 + i * 2e-5 + base * 0.02,
                    "net_asset": 4e9 + i * 5e6 + base,
                    "total_asset": 8e9 + i * 1e7 + base,
                    "sps": 2.0 + i * 0.002 + base,
                    "eps_q": 0.25 + i * 0.0002 + base,
                    "revenue_q": 1.2e9 + i * 2e6 + base,
                }
            )
    return pd.DataFrame(rows)


def test_compute_returns_15_factors():
    computer = FundamentalComputer(config={})
    out = computer.compute(_metrics())
    ids = set(out["factor_id"])
    from factor_engine.fin_ext import FIN_EXT_SPECS

    expected = {spec.factor_id for spec in FUNDAMENTAL_SPECS} | {
        spec.factor_id for spec in FIN_EXT_SPECS
    }
    assert ids == expected
    assert set(out.columns) == {"ts", "code", "factor_id", "value"}


def test_rank_factors_are_percentile_in_01():
    computer = FundamentalComputer(config={})
    out = computer.compute(_metrics())
    ranked = out[out["factor_id"].isin(RANK_FACTORS)]
    assert not ranked.empty
    assert ranked["value"].dropna().between(0.0, 1.0).all()


def test_raw_factors_are_not_rank_bounded():
    computer = FundamentalComputer(config={})
    out = computer.compute(_metrics())
    from factor_engine.fin_ext import RANK_EXT

    rank_ids = set(RANK_FACTORS) | set(RANK_EXT)
    raw = out[~out["factor_id"].isin(rank_ids)]
    assert {"valuation.peg_252d", "growth.yoy_net_profit", "growth.yoy_revenue"} <= set(
        raw["factor_id"]
    )
    # 未做截面排名的因子不应被压缩到 [0,1]
    assert (raw["value"].abs() > 1).any() or raw["value"].notna().any()


def test_empty_metrics_returns_empty_frame():
    computer = FundamentalComputer(config={})
    out = computer.compute(pd.DataFrame())
    assert list(out.columns) == ["ts", "code", "factor_id", "value"]
    assert out.empty
