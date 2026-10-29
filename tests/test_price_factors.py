"""价量因子落地测试：自研命名、分类注册、数值正确性。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import factors
from factors import F, build_panel
from factors.cross_section import nonlinear_size
from factors.naming import house_id
from factors.ops.price_factors import ALL_NAMES, CROSS_SECTION_ONLY, GROUPS, lookup

def _frame(days: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=days)
    price = 10 * np.cumprod(1 + rng.normal(0, 0.015, days))
    volume = rng.integers(1_000_000, 5_000_000, days).astype(float)
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": "600000",
            "open": price * 0.995,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": volume,
            "amount": volume * price,
            "turnover": volume / 1e8 * 100,
            "adj_factor": 1.0,
            "float_mv": 5e9,
            "total_mv": 6e9,
            "float_share": 5e8,
        }
    )

def _series(factor_id: str, frame: pd.DataFrame) -> pd.Series:
    fn, _window, _unit = lookup(factor_id)
    data = frame.copy()
    data["hfq_close"] = data["close"]
    data["hfq_high"] = data["high"]
    data["hfq_low"] = data["low"]
    return fn(data)

# ---------------------------------------------------------------- 命名
def test_names_follow_house_style():
    assert house_id("liquidity", "avg_turnover_5d") == "liquidity.turnover_ma_5d"
    assert house_id("momentum", "return_21d") == "momentum.ret_21d"
    assert house_id("momentum", "MACD") == "momentum.macd"
    assert house_id("risk", "sigma_1320d_000300") == "risk.ivol_1320d_csi300"
    assert house_id("size", "size") == "size.mv"
    for name in ALL_NAMES:
        assert "000300" not in name and "000001" not in name

def test_all_names_unique():
    assert len(ALL_NAMES) == len(set(ALL_NAMES))

def test_factor_ids_lowercase_with_category():
    for name in ALL_NAMES:
        assert name == name.lower()
        category, _, short = name.partition(".")
        assert category in {"liquidity", "momentum", "reversal", "risk", "size"}
        assert short

# ---------------------------------------------------------------- 注册
def test_registered_counts():
    assert sum(len(group) for group in GROUPS.values()) == 85
    assert len(CROSS_SECTION_ONLY) == 1
    assert len(ALL_NAMES) == 86
    for name in ALL_NAMES:
        if name == house_id("size", CROSS_SECTION_ONLY[0]):
            continue
        assert name in factors.CUSTOM_FACTORS

# ---------------------------------------------------------------- 数值
def test_return_and_ma():
    frame = _frame()
    got = _series(house_id("momentum", "return_21d"), frame)
    expected = frame["close"] / frame["close"].shift(21) - 1
    assert np.allclose(got.dropna(), expected.dropna())

    ma = _series(house_id("momentum", "ma_20d"), frame)
    assert np.allclose(ma.dropna(), frame["close"].rolling(20, min_periods=5).mean().dropna())

def test_rsi_bounds():
    rsi = _series(house_id("reversal", "rsi"), _frame()).dropna()
    assert len(rsi) > 50
    assert rsi.min() >= 0 and rsi.max() <= 100

def test_high_low_ratio():
    ratio = _series(house_id("risk", "high_low_21d"), _frame()).dropna()
    assert (ratio >= 1 - 1e-9).all()

def test_macd_identity():
    frame = _frame()
    dif = _series(house_id("momentum", "dif"), frame)
    dea = _series(house_id("momentum", "dea"), frame)
    macd = _series(house_id("momentum", "MACD"), frame)
    assert np.allclose((2 * (dif - dea)).dropna(), macd.dropna())

def test_size_factors():
    frame = _frame()
    assert np.allclose(
        _series(house_id("size", "size"), frame).to_numpy(),
        -np.log(frame["total_mv"] / 1e6).to_numpy(),
    )
    assert np.allclose(
        _series(house_id("size", "float_size"), frame).to_numpy(),
        -np.log(frame["float_mv"] / 1e6).to_numpy(),
    )

def test_turnover_factors():
    frame = _frame()
    got = _series(house_id("liquidity", "avg_turnover_5d"), frame)
    expected = frame["turnover"].rolling(5, min_periods=2).mean()
    assert np.allclose(got.dropna(), expected.dropna())

    bias = _series(house_id("liquidity", "bias_turn_21d_252d"), _frame(days=600))
    assert bias.notna().sum() > 50

def test_benchmark_required_raises():
    frame = _frame()
    data = frame.copy()
    data["hfq_close"] = data["close"]
    with pytest.raises(ValueError, match="benchmark"):
        lookup(house_id("risk", "beta_60d_000300"))[0](data)

def test_beta_with_benchmark():
    frame = _frame()
    data = frame.copy()
    data["hfq_close"] = data["close"]
    rng = np.random.default_rng(3)
    data["benchmark_close"] = 3000 * np.cumprod(1 + rng.normal(0, 0.01, len(data)))
    beta = lookup(house_id("risk", "beta_60d_000300"))[0](data).dropna()
    assert len(beta) > 100
    assert beta.abs().max() < 10

def test_nonlinear_size_residual():
    rng = np.random.default_rng(11)
    dates = pd.date_range("2026-01-01", periods=5)
    cols = [f"s{i}" for i in range(30)]
    size = pd.DataFrame(rng.normal(0, 1, (5, 30)), index=dates, columns=cols)
    residual = nonlinear_size(size)
    for dt in dates:
        pair = pd.concat([size.loc[dt], residual.loc[dt]], axis=1).dropna()
        assert abs(pair.corr().iloc[0, 1]) < 1e-6

def test_build_panel_end_to_end():
    frame = _frame(days=120)
    defs = [F.ts(house_id("momentum", "return_21d")), F.ts(house_id("reversal", "rsi"))]
    panel = build_panel(frame, defs, frame["date"].min(), frame["date"].max())
    assert panel.values[house_id("momentum", "return_21d")].notna().sum().sum() > 0

def test_unknown_factor_raises():
    with pytest.raises(ValueError):
        F.ts("not.a.factor")
