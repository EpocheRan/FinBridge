"""因子研究报告核心指标的单元测试（合成数据，不连数据库）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.report import metrics
from research.report import diagnostics as diag


def _panel(n_dates: int = 60, n_cols: int = 40, seed: int = 0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    cols = [f"{i:06d}" for i in range(n_cols)]
    close = pd.DataFrame(
        10 * np.cumprod(1 + rng.normal(0, 0.01, size=(n_dates, n_cols)), axis=0),
        index=dates, columns=cols,
    )
    open_ = close * (1 + rng.normal(0, 0.002, size=(n_dates, n_cols)))
    factor = pd.DataFrame(rng.normal(size=(n_dates, n_cols)), index=dates, columns=cols)
    return dates, cols, close, open_, factor


def test_independent_t_reduces_sample():
    series = pd.Series(np.random.default_rng(1).normal(0.02, 0.1, size=500))
    t_all = metrics.newey_west_t(series, lag=4)
    t_ind, n_ind = metrics.independent_t(series, step=5)
    assert n_ind == 100
    assert np.isfinite(t_ind)
    assert abs(t_ind) < abs(t_all) * 1.5


def test_tradeability_counts_suspension_and_limit_up():
    dates, cols, close, open_, factor = _panel(n_dates=40, n_cols=40)
    # t=10 的 signal，t+1 停牌
    open_.iloc[11, 0] = np.nan
    # t=20 的 signal，t+1 一字涨停（10 → 11）
    close.iloc[20, 1] = 10.0
    open_.iloc[21, 1] = 11.0
    out = metrics._tradeability(factor, close, open_, horizon=5, is_st={})
    assert out["denominator"] == 39 * 40
    assert out["suspended"] == 1
    assert out["limit_up"] == 1
    assert out["blocked"] == 2
    assert out["old_denominator"] == 40 * 40
    assert out["old_pct"] > out["pct"]


def test_delayed_exit_extends_on_limit_down():
    dates = pd.bdate_range("2024-01-02", periods=20)
    cols = ["000001"]
    close = pd.DataFrame(10.0, index=dates, columns=cols)
    open_ = pd.DataFrame(10.0, index=dates, columns=cols)
    close.iloc[10, 0] = 9.0  # 前收 10，跌停 9.0
    close.iloc[11, 0] = 9.2
    ret, delay, blocked = metrics._delayed_exit(close, open_, 5)
    assert bool(blocked.iloc[5, 0])
    assert delay.iloc[5, 0] == 1
    assert np.isclose(ret.iloc[5, 0], 9.2 / 10.0 - 1)


def test_neutralize_removes_size_and_industry():
    rng = np.random.default_rng(3)
    n_dates, n_cols = 120, 120
    dates = pd.bdate_range("2024-01-02", periods=n_dates)
    cols = [f"{i:06d}" for i in range(n_cols)]
    cap = pd.DataFrame(
        np.exp(rng.normal(10, 1, size=(n_dates, n_cols))), index=dates, columns=cols
    )
    industry = pd.DataFrame(
        [[f"ind{i % 6}" for i in range(n_cols)]] * n_dates, index=dates, columns=cols
    )
    factor = pd.DataFrame(
        2.0 * np.log(cap.to_numpy())
        + np.array([int(c) % 6 for c in cols])[None, :] * 3.0
        + rng.normal(0, 0.5, size=(n_dates, n_cols)),
        index=dates, columns=cols,
    )
    resid = metrics.neutralize_panel(factor, cap, industry, mode="both")
    # 残差与 log 市值的截面相关应显著下降
    raw_corr = np.nanmean([
        factor.iloc[i].rank().corr(np.log(cap.iloc[i]).rank()) for i in range(n_dates)
    ])
    res_corr = np.nanmean([
        resid.iloc[i].rank().corr(np.log(cap.iloc[i]).rank()) for i in range(n_dates)
    ])
    assert abs(res_corr) < abs(raw_corr) * 0.5
    # 行业内残差均值接近 0
    last = industry.iloc[-1]
    means = resid.iloc[-1].groupby(last.to_numpy()).mean().abs().max()
    assert means < 1e-8


def test_performance_mdd_and_annual():
    nav = pd.Series(
        [1.0, 1.1, 0.99, 1.2],
        index=pd.to_datetime(["2024-01-02", "2024-06-03", "2024-09-02", "2024-12-02"]),
    )
    perf = metrics._performance(nav)
    assert np.isclose(perf["mdd"], 0.99 / 1.1 - 1)
    annual = metrics._annual_returns(nav)
    assert np.isclose(annual.iloc[0], 0.2)


def test_filter_test_positive_q1_is_not_useful():
    rng = np.random.default_rng(5)
    n = 120
    base = rng.normal(0.002, 0.01, n)
    periods = pd.DataFrame(
        {
            "Q1": base + 0.0002,
            "Q2": base + 0.0004,
            "Q3": base + 0.0005,
            "Q4": base + 0.0006,
            "Q5": base + 0.0008,
        }
    )
    out = metrics._filter_test(periods, 5)
    assert out["q1_mean"] > 0
    assert out["spread_mean"] > 0
    assert not out["useful"]


def test_build_report_smoke_and_direction_banner_data():
    dates, cols, close, open_, factor = _panel(n_dates=160, n_cols=80)
    cap = pd.DataFrame(1e9, index=dates, columns=cols)
    industry = pd.DataFrame(
        [[f"ind{i % 8}" for i in range(len(cols))]] * len(dates), index=dates, columns=cols
    )
    regime = pd.Series(
        np.where(np.arange(len(dates)) % 40 < 20, "bull", "bear"), index=dates
    )
    fwd = close.shift(-5) / open_.shift(-1) - 1
    factor = -fwd  # 设计方向为负：取负后恰好预测未来收益
    report = metrics.build_report(
        factor, close, open_, factor_id="liquidity.test", category="liquidity",
        direction=-1, regime=regime, market_cap=cap, industry=industry,
        definition={"unit": "元"}, full=False,
    )
    assert report.direction == -1
    assert report.raw_direction == -1
    assert not report.flipped
    assert set(["t_indep", "n_indep", "ci_lo"]).issubset(report.ic_stats.columns)
    assert report.tradeability["denominator"] > 0


def test_no_in_sample_direction_flip():
    """方向由先验决定；IC 为负时不得翻转，否则等于样本内择向。"""
    dates, cols, close, open_, factor = _panel(n_dates=180, n_cols=60)
    fwd = close.shift(-5) / open_.shift(-1) - 1
    report = metrics.build_report(
        -fwd, close, open_, factor_id="momentum.test", category="momentum",
        direction=1, full=False,
    )
    assert report.direction == 1
    assert report.flipped is False
    assert report.ic[5].mean() < 0
    # 方向断言应触发 fail，而不是把 IC 翻成正数
    direction_check = {c.check_id: c for c in report.diagnostics}["direction"]
    assert direction_check.status == "fail"


def test_rho_spread_hard_boundary_only_at_extreme():
    assert diag.rho_spread_identity(0.9, -0.01).status == "fail"
    assert diag.rho_spread_identity(-0.9, 0.01).status == "fail"
    # 中间区间允许非单调排列，只报警不判失败
    assert diag.rho_spread_identity(0.2, -0.01).status == "warn"
    assert diag.rho_spread_identity(0.9, 0.01).status == "pass"


def test_tradeability_interval_has_no_dead_zone():
    cost = 0.002
    below = {"mean": 0.0005, "ci_lo": -0.0005, "ci_hi": 0.0015}
    above = {"mean": 0.0035, "ci_lo": 0.0025, "ci_hi": 0.0045}
    straddle = {"mean": 0.0020, "ci_lo": 0.0005, "ci_hi": 0.0035}
    assert diag.tradeability_interval(below, cost).status == "fail"
    assert diag.tradeability_interval(above, cost).status == "pass"
    assert diag.tradeability_interval(straddle, cost).status == "warn"


def test_negative_retention_has_plain_wording():
    text = diag.retention_note(-0.15, 0.99)
    assert "残差为负" in text
    assert "0 分" in text
    assert "-15%" not in text


def test_direction_error_gets_attribution():
    dates, cols, close, open_, factor = _panel(n_dates=220, n_cols=60)
    fwd = close.shift(-5) / open_.shift(-1) - 1
    report = metrics.build_report(
        -fwd, close, open_, factor_id="quality.test", category="quality",
        direction=1, full=False,
    )
    assert report.attribution["label"] == "方向错"
