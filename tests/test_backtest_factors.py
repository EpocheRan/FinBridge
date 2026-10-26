import numpy as np
import pandas as pd
import pytest

from factors import F, FactorError, build_panel, required_warmup


def _frame(days: int = 120, symbols=("AAA", "BBB")) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for symbol, base in zip(symbols, (10.0, 20.0)):
        for i, dt in enumerate(dates):
            close = base + i * 0.05 + np.sin(i / 7.0)
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 10_000,
                    "adj_factor": 1.0,
                    "hfq_close": close,
                }
            )
    return pd.DataFrame(rows)


def test_factor_panel_values_and_warmup():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    panel = build_panel(frame, [F.mom(20), F.ma(20), F.atr(14)], start, end)
    assert panel.symbols == ("AAA", "BBB")
    assert set(panel.values) == {"mom20", "ma20", "atr14"}
    assert panel.get("AAA", "atr14", frame["date"].max()) > 0
    assert panel.meta["mom20"].warmup == 20
    assert panel.meta["atr14"].price_basis == "hfq"
    assert required_warmup([F.mom(20), F.ma(60), F.stored("pct_chg", unit="pct")]) == 60


def test_factor_no_lookahead_on_truncated_data():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    cutoff = pd.Timestamp("2024-04-15")

    full = build_panel(frame, [F.mom(20), F.ma(20), F.atr(14)], start, end)
    truncated = build_panel(
        frame[frame["date"] <= cutoff], [F.mom(20), F.ma(20), F.atr(14)], start, end
    )
    for name in ("mom20", "ma20", "atr14"):
        left = full.values[name].loc[:cutoff]
        right = truncated.values[name].loc[:cutoff]
        pd.testing.assert_frame_equal(left, right, check_exact=False, rtol=1e-12, atol=1e-12)


def test_cross_section_rank_is_per_date():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    panel = build_panel(frame, [F.mom(20), F.rank("mom20")], start, end)
    last = frame["date"].max()
    ranks = panel.at(last, "rank_mom20")
    assert sorted(ranks.dropna().tolist()) == [0.5, 1.0]


def test_stored_price_unit_is_rejected():
    with pytest.raises(ValueError):
        F.stored("atr14", unit="price")
    with pytest.raises(ValueError):
        F.stored("close", unit="price")


def test_stored_requires_future_source_implementation():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    with pytest.raises(FactorError):
        build_panel(frame, [F.stored("pct_chg", unit="pct")], start, end)
