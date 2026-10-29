"""多因子合成（等权 rank）与三个细节：方向统一 / 中性化 / 缺失值。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factors import F, FactorError, build_panel, required_warmup
from factors import cross_section


def _panel(values: dict[str, list[float]], dates=None) -> pd.DataFrame:
    dates = dates or pd.date_range("2026-01-01", periods=len(next(iter(values.values()))))
    return pd.DataFrame(values, index=dates)


# ---------------------------------------------------------------- 方向统一
def test_signed_rank_flips_for_negative_direction():
    panel = _panel({"a": [1.0, 2.0, 3.0, 4.0], "b": [4.0, 3.0, 2.0, 1.0]})
    positive = cross_section.signed_rank(panel, 1)
    negative = cross_section.signed_rank(panel, -1)
    # 正向：值越大分数越高；负向反之
    assert positive["a"].is_monotonic_increasing
    assert negative["a"].is_monotonic_decreasing
    # 两者互补，且都落在 (0, 1]
    assert np.allclose(positive.to_numpy() + negative.to_numpy(), 1.0)


def test_composite_equal_weight_is_bounded():
    f1 = _panel({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0], "c": [2.0, 3.0, 1.0]})
    f2 = _panel({"a": [3.0, 1.0, 2.0], "b": [1.0, 3.0, 2.0], "c": [1.0, 2.0, 3.0]})
    score = cross_section.composite([f1, f2], [1.0, 1.0], [1, 1])
    assert score.shape == f1.shape
    assert score.min().min() > 0
    assert score.max().max() <= 1.0


def test_composite_negative_direction_equals_flipped_rank():
    f1 = _panel({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0], "c": [2.0, 3.0, 1.0]})
    flipped = cross_section.composite([f1], [1.0], [-1])
    direct = cross_section.composite([-f1], [1.0], [1])
    # rank_pct 的端点差异会带来 1/n 的常数偏移，比较排序一致即可
    assert (flipped.rank(axis=1).to_numpy() == direct.rank(axis=1).to_numpy()).all()


# ---------------------------------------------------------------- 缺失值
def test_missing_component_renormalizes_instead_of_zero_fill():
    """缺一个分量时，分数应等于"只用可用分量"，不能被 0 拉低。"""
    f1 = _panel({"a": [1.0], "b": [2.0], "c": [3.0]})
    f2 = _panel({"a": [9.0], "b": np.nan, "c": [1.0]})
    score = cross_section.composite([f1, f2], [1.0, 1.0], [1, 1])
    only_f1 = cross_section.signed_rank(f1, 1)
    # b 只有 f1 可用 → 归一后等于 f1 自己的秩
    assert score.loc[:, "b"].iloc[0] == pytest.approx(only_f1.loc[:, "b"].iloc[0])
    # a/c 两个分量都可用 → 是两者的均值，不是"缺一个就变 0"
    expected_a = (only_f1.loc[:, "a"].iloc[0] + cross_section.signed_rank(f2, 1).loc[:, "a"].iloc[0]) / 2
    assert score.loc[:, "a"].iloc[0] == pytest.approx(expected_a)


def test_min_parts_requires_enough_components():
    f1 = _panel({"a": [1.0, 2.0], "b": [2.0, 1.0]})
    f2 = _panel({"a": [np.nan, np.nan], "b": [1.0, 2.0]})
    f3 = _panel({"a": [1.0, 2.0], "b": np.nan})
    score = cross_section.composite([f1, f2, f3], min_parts=2)
    # a 只有 f1,f3 → 2 个可用；b 只有 f1,f2 → 2 个可用；都满足
    assert score.notna().all().all()
    strict = cross_section.composite([f1, f2, f3], min_parts=3)
    assert strict.isna().all().all()


def test_composite_rejects_bad_weights():
    f1 = _panel({"a": [1.0]})
    with pytest.raises(ValueError):
        cross_section.composite([f1], [1.0, 2.0])


# ---------------------------------------------------------------- 中性化
def test_neutralize_removes_industry_offset():
    dates = pd.date_range("2026-01-01", periods=3)
    symbols = [f"s{i}" for i in range(8)]
    industry = pd.DataFrame(
        [["bank"] * 4 + ["tech"] * 4] * 3, index=dates, columns=symbols
    )
    base = np.tile(np.array([10.0, 10.1, 10.2, 10.3, 0.0, 0.1, 0.2, 0.3]), (3, 1))
    panel = pd.DataFrame(base, index=dates, columns=symbols)
    residual = cross_section.neutralize(panel, industry=industry, min_obs=5)
    # 每个行业内部的均值应被消掉
    assert abs(residual[symbols[:4]].mean(axis=1).max()) < 1e-9
    assert abs(residual[symbols[4:]].mean(axis=1).max()) < 1e-9


def test_neutralize_falls_back_when_too_few_obs():
    dates = pd.date_range("2026-01-01", periods=1)
    panel = pd.DataFrame([[1.0, 2.0, 3.0]], index=dates, columns=["a", "b", "c"])
    industry = pd.DataFrame([["x", "y", "z"]], index=dates, columns=["a", "b", "c"])
    residual = cross_section.neutralize(panel, industry=industry, min_obs=10)
    assert residual.mean(axis=1).iloc[0] == pytest.approx(0.0, abs=1e-12)


def test_neutralize_keeps_nan():
    dates = pd.date_range("2026-01-01", periods=1)
    panel = pd.DataFrame([[1.0, np.nan, 3.0]], index=dates, columns=["a", "b", "c"])
    residual = cross_section.neutralize(panel)
    assert np.isnan(residual.loc[dates[0], "b"])


# ---------------------------------------------------------------- 端到端
def _price_frame(symbols=("600000", "600001", "600002", "600003"), days=40) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    dates = pd.bdate_range("2026-01-05", periods=days)
    for code in symbols:
        price = 10.0
        for dt in dates:
            price *= 1 + rng.normal(0, 0.02)
            rows.append(
                {
                    "date": dt,
                    "symbol": code,
                    "open": price * 0.99,
                    "high": price * 1.01,
                    "low": price * 0.98,
                    "close": price,
                    "volume": 1_000_000,
                    "adj_factor": 1.0,
                }
            )
    return pd.DataFrame(rows)


def test_build_panel_composite_end_to_end():
    frame = _price_frame()
    defs = [
        F.mom(5, name="mom5"),
        F.vol(5, name="vol5"),
        F.composite([("mom5", 1, 0.6), ("vol5", -1, 0.4)], name="score_rev"),
    ]
    panel = build_panel(frame, defs, frame["date"].min(), frame["date"].max())
    score = panel.values["score_rev"]
    assert score.shape == panel.values["mom5"].shape
    valid = score.notna().sum().sum()
    assert valid > 0
    # 分数在 (0,1] 内
    assert np.nanmin(score.to_numpy()) > 0
    assert np.nanmax(score.to_numpy()) <= 1.0
    assert panel.meta["score_rev"].source == "computed"


def test_required_warmup_propagates_through_composite():
    defs = [F.mom(20, name="mom20"), F.composite([("mom20", 1.0)], name="score")]
    assert required_warmup(defs) == 20


def test_composite_with_neutralize_requires_reference():
    frame = _price_frame()
    defs = [
        F.mom(5, name="mom5"),
        F.composite([("mom5", 1.0)], name="score", neutralize=("industry",)),
    ]
    with pytest.raises(FactorError):
        build_panel(frame, defs, frame["date"].min(), frame["date"].max())


def test_composite_with_neutralize_uses_reference():
    frame = _price_frame(symbols=("600000", "600001", "600002", "600003", "600004", "600005"))
    symbols = sorted(frame["symbol"].unique())
    dates = sorted(frame["date"].unique())
    industry = pd.DataFrame(
        [["bank"] * 3 + ["tech"] * 3] * len(dates), index=pd.DatetimeIndex(dates), columns=symbols
    )
    size = pd.DataFrame(
        np.tile(np.array([100.0, 200.0, 300.0, 100.0, 200.0, 300.0]), (len(dates), 1)),
        index=pd.DatetimeIndex(dates),
        columns=symbols,
    )
    defs = [
        F.mom(5, name="mom5"),
        F.composite(
            [("mom5", 1.0)],
            name="score_neu",
            neutralize=("industry", "size"),
        ),
    ]
    panel = build_panel(
        frame,
        defs,
        frame["date"].min(),
        frame["date"].max(),
        reference={"industry": industry, "size": size},
    )
    assert panel.values["score_neu"].notna().sum().sum() > 0


def test_bad_composite_specs():
    with pytest.raises(ValueError):
        F.composite([])
    with pytest.raises(ValueError):
        F.composite(["a"], min_parts=0)
    with pytest.raises(ValueError):
        F.composite([("a", 1, 1.0, "extra")])
    with pytest.raises(ValueError):
        F.composite(["a"], winsorize=0.6)


def test_composite_missing_dependency_raises():
    frame = _price_frame()
    defs = [F.mom(5, name="mom5"), F.composite(["not_exists"], name="score")]
    with pytest.raises(FactorError):
        build_panel(frame, defs, frame["date"].min(), frame["date"].max())


def test_winsorize_clips_outliers():
    panel = _panel({"a": [1.0, 2.0, 3.0, 100.0], "b": [1.0, 2.0, 3.0, 4.0]})
    clipped = cross_section.winsorize(panel, 0.25)
    assert clipped.max().max() < 100.0
