"""公司行为近似：adj_factor 变动 -> 股数比例调整。"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.application.corporate_actions import derive_approximate_actions
from backtest.infrastructure.execution.ledger import PortfolioLedger


def _frame() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=3)
    closes = [10.0, 11.0, 5.5]
    factors = [1.0, 1.0, 2.0]
    return pd.DataFrame(
        {
            "symbol": "AAA",
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": 1000,
            "adj_factor": factors,
            "hfq_close": [c * f for c, f in zip(closes, factors)],
        }
    )


def test_derive_approximate_actions_from_factor_jump():
    actions = derive_approximate_actions(_frame())
    assert len(actions) == 1
    row = actions.iloc[0]
    assert row["date"] == pd.Timestamp("2024-01-04")
    assert row["symbol"] == "AAA"
    assert row["cash_div"] == 0.0
    assert row["share_ratio"] == pytest.approx(1.0)


def test_portfolio_ledger_applies_share_ratio_action():
    frame = _frame()
    orders = pd.DataFrame(
        [
            {
                "id": 1,
                "type": "buy",
                "symbol": "AAA",
                "date": pd.Timestamp("2024-01-02"),
                "shares": 100,
                "fill_price": 10.0,
                "fees": 0.0,
            }
        ]
    )
    actions = derive_approximate_actions(frame)
    result = PortfolioLedger(
        initial_cash=10_000,
        data=frame,
        orders=orders,
        symbols=("AAA",),
        corporate_actions=actions,
    ).run()
    equity = result.equity.set_index("date")["equity"]
    # 1 拆 2 后：100 股 -> 200 股，价格 11 -> 5.5，市值保持 1100。
    assert equity.loc[pd.Timestamp("2024-01-04")] == pytest.approx(10_100)
