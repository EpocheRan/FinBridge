"""因子研究指标测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.infrastructure.data.memory import MemoryDataSource
from backtest.research import (
    decay,
    forward_returns,
    ic,
    ic_summary,
    parse_factor_name,
    quantile_returns,
    run_research,
    turnover,
)


def _prices() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=5)
    return pd.DataFrame(
        {
            "AAA": [10.0, 11.0, 12.0, 13.0, 14.0],
            "BBB": [20.0, 19.0, 18.0, 17.0, 16.0],
            "CCC": [30.0, 30.0, 31.0, 32.0, 33.0],
        },
        index=dates,
    )


def test_forward_returns_are_future_returns():
    close = _prices()
    result = forward_returns(close, horizons=(1, 2))
    assert result[1].loc[close.index[0], "AAA"] == pytest.approx(0.1)
    assert result[1].loc[close.index[0], "BBB"] == pytest.approx(-0.05)
    assert result[1].loc[close.index[0], "CCC"] == pytest.approx(0.0)
    assert result[2].loc[close.index[0], "AAA"] == pytest.approx(0.2)
    assert pd.isna(result[1].loc[close.index[-1], "AAA"])


def test_ic_and_summary_with_perfect_factor():
    close = _prices()
    forward = forward_returns(close, horizons=(1,))[1]
    dates = close.index[:2]
    factor = pd.DataFrame(
        {
            "AAA": [2.0, 2.0],
            "BBB": [0.0, 0.0],
            "CCC": [1.0, 1.0],
        },
        index=dates,
    )
    series = ic(factor, forward)
    assert series.tolist() == pytest.approx([1.0, 1.0])
    summary = ic_summary(series)
    assert summary["count"] == 2
    assert summary["mean"] == pytest.approx(1.0)
    assert summary["win_rate"] == pytest.approx(1.0)
    assert summary["t_stat"] >= 0.0


def test_quantile_returns_and_turnover():
    close = _prices()
    forward = forward_returns(close, horizons=(1,))[1]
    factor = pd.DataFrame(
        {
            "AAA": [3.0, 3.0, 3.0, 3.0],
            "BBB": [2.0, 1.0, 4.0, 4.0],
            "CCC": [1.0, 2.0, 2.0, 2.0],
        },
        index=close.index[:4],
    )
    quantiles = quantile_returns(factor, forward, q=2)
    assert "q1" in quantiles.columns
    assert "q2" in quantiles.columns
    assert "long_short" in quantiles.columns
    assert not quantiles.empty
    turn = turnover(factor, top_n=1)
    assert turn.tolist() == pytest.approx([0.0, 0.0, 1.0, 0.0])


def test_decay_returns_one_row_per_horizon():
    close = _prices()
    factor = close.pct_change().fillna(0.0)
    result = decay(factor, close, horizons=(1, 2))
    assert result.index.tolist() == [1, 2]
    assert "mean" in result.columns


def test_parse_factor_name():
    assert parse_factor_name("mom20").name == "mom20"
    assert parse_factor_name("ATR14").name == "atr14"
    with pytest.raises(ValueError):
        parse_factor_name("unknown")


def test_run_research_with_memory_source(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=60)
    rows = []
    for index, dt in enumerate(dates):
        for offset, symbol in enumerate(("AAA", "BBB", "CCC")):
            base = 10.0 + offset * 2.0
            close = base + index * (0.1 + offset * 0.02) + (index % 3) * 0.01
            rows.append(
                {
                    "date": dt,
                    "symbol": symbol,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1000.0,
                    "adj_factor": 1.0,
                }
            )
    report = run_research(
        data_source=MemoryDataSource(pd.DataFrame(rows)),
        codes=("AAA", "BBB", "CCC"),
        factor="mom5",
        start=dates[10].date(),
        end=dates[-2].date(),
        horizons=(1, 5),
        q=2,
        top_n=2,
    )
    assert report.horizons == (1, 5)
    assert report.summary.index.tolist() == [1, 5]
    assert set(report.ic) == {1, 5}
    assert not report.turnover.empty
    assert not report.decay.empty
    assert "q1" in report.quantiles[1].columns
    target = report.write_csv(tmp_path)
    assert (target / "ic_summary.csv").exists()
    assert (target / "quantiles_1.csv").exists()
    assert (target / "turnover.csv").exists()
