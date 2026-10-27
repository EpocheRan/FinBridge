"""多标的账本：回放、停牌估值、公司行为钩子、对账。"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.infrastructure.execution.ledger import (
    PortfolioLedger,
    SimpleLedger,
    reconcile,
)


def _data(*, skip_aaa_on_day3: bool = False) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=4)
    prices = {
        "AAA": [10.0, 11.0, 12.0, 13.0],
        "BBB": [20.0, 19.0, 18.0, 17.0],
    }
    rows = []
    for symbol, closes in prices.items():
        for index, dt in enumerate(dates):
            if skip_aaa_on_day3 and symbol == "AAA" and index == 2:
                continue
            close = closes[index]
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000,
                    "amount": close * 1000,
                    "adj_factor": 1.0,
                    "hfq_close": close,
                }
            )
    return pd.DataFrame(rows)


def _orders() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=4)
    return pd.DataFrame(
        [
            {
                "id": 1,
                "type": "buy",
                "symbol": "AAA",
                "date": dates[0],
                "shares": 100,
                "fill_price": 10.0,
                "fees": 0.0,
            },
            {
                "id": 2,
                "type": "buy",
                "symbol": "BBB",
                "date": dates[1],
                "shares": 50,
                "fill_price": 20.0,
                "fees": 0.0,
            },
            {
                "id": 3,
                "type": "sell",
                "symbol": "AAA",
                "date": dates[3],
                "shares": 100,
                "fill_price": 12.0,
                "fees": 0.0,
            },
        ]
    )


def test_portfolio_ledger_replays_multi_symbol_orders():
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(),
        orders=_orders(),
        symbols=("AAA", "BBB"),
    ).run()
    frame = result.equity.set_index("date")

    assert frame.loc[pd.Timestamp("2024-01-02"), "equity"] == pytest.approx(10_000)
    assert frame.loc[pd.Timestamp("2024-01-03"), "equity"] == pytest.approx(10_050)
    assert frame.loc[pd.Timestamp("2024-01-05"), "equity"] == pytest.approx(10_050)
    assert result.metrics["total_return_pct"] == pytest.approx(0.5)
    assert not result.metrics.get("max_drawdown_pct", 0) > 0


def test_portfolio_ledger_uses_last_close_on_suspension():
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(skip_aaa_on_day3=True),
        orders=_orders(),
        symbols=("AAA", "BBB"),
    ).run()
    frame = result.equity.set_index("date")
    # AAA 停牌：沿用 11.0；BBB 18 * 50
    assert frame.loc[pd.Timestamp("2024-01-04"), "market_value"] == pytest.approx(
        100 * 11.0 + 50 * 18.0
    )


def test_portfolio_ledger_deducts_fees():
    orders = _orders()
    orders.loc[orders["id"] == 1, "fees"] = 5.0
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(),
        orders=orders,
        symbols=("AAA", "BBB"),
    ).run()
    frame = result.equity.set_index("date")
    assert frame.loc[pd.Timestamp("2024-01-02"), "cash"] == pytest.approx(8_995)


def test_corporate_action_hook_adds_cash_dividend():
    actions = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-03"),
                "symbol": "AAA",
                "cash_div": 1.0,
                "share_ratio": 0.0,
            }
        ]
    )
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(),
        orders=_orders(),
        symbols=("AAA", "BBB"),
        corporate_actions=actions,
    ).run()
    frame = result.equity.set_index("date")
    # day2：100 股 * 1 元分红
    assert frame.loc[pd.Timestamp("2024-01-03"), "cash"] == pytest.approx(8_100)


def test_simple_ledger_matches_single_symbol_portfolio_ledger():
    simple = SimpleLedger(
        initial_cash=10_000,
        data=_data()[_data()["symbol"] == "AAA"],
        orders=_orders()[_orders()["symbol"] == "AAA"],
        symbol="AAA",
    ).run()
    portfolio = PortfolioLedger(
        initial_cash=10_000,
        data=_data()[_data()["symbol"] == "AAA"],
        orders=_orders()[_orders()["symbol"] == "AAA"],
        symbols=("AAA",),
    ).run()
    pd.testing.assert_frame_equal(simple.equity, portfolio.equity)


def test_portfolio_ledger_computes_alpha_beta_with_benchmark():
    dates = pd.bdate_range("2024-01-02", periods=4)
    benchmark = pd.Series(
        [100.0, 101.0, 102.0, 103.0],
        index=dates,
        name="000300",
    )
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(),
        orders=_orders(),
        symbols=("AAA", "BBB"),
        benchmark=benchmark,
        risk_free_rate=0.02,
    ).run()
    for key in (
        "beta",
        "alpha_annualized_pct",
        "correlation",
        "tracking_error_pct",
        "information_ratio",
        "benchmark_return_pct",
        "excess_return_pct",
        "benchmark_points",
    ):
        assert key in result.metrics, key
    assert result.metrics["benchmark_points"] == pytest.approx(4.0)


def test_portfolio_ledger_without_benchmark_has_no_alpha_beta():
    result = PortfolioLedger(
        initial_cash=10_000,
        data=_data(),
        orders=_orders(),
        symbols=("AAA", "BBB"),
    ).run()
    assert "beta" not in result.metrics
    assert "alpha_annualized_pct" not in result.metrics


def test_reconcile_reports_relative_tolerance():
    ledger = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=2),
            "equity": [100_000.0, 101_000.0],
        }
    )
    pybroker = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=2),
            "equity": [100_000.0, 101_000.5],
        }
    )
    result = reconcile(ledger, pybroker, tolerance=1e-6)
    assert result["max_abs_diff"] == pytest.approx(0.5)
    assert result["max_rel_diff"] == pytest.approx(0.5 / 101_000.5, rel=1e-9)
    assert result["exceeds_tolerance"] == 1.0
