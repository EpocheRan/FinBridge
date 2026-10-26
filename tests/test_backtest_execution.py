import pandas as pd
import pytest

from backtest.domain.models import ExecutionRules
from backtest.infrastructure.execution.a_share import (
    FeeModel,
    TPlusOneLedger,
    can_buy,
    can_sell,
    limit_prices,
    round_lot,
)


def _row(close: float, volume: float = 1000.0) -> dict:
    return {"close": close, "volume": volume}


def test_round_lot():
    assert round_lot(199, 100) == 100
    assert round_lot(200, 100) == 200
    assert round_lot(0, 100) == 0


def test_limit_prices_by_board():
    assert limit_prices(100, "600519") == (90.0, 110.0)
    assert limit_prices(100, "300750") == (80.0, 120.0)
    assert limit_prices(100, "688001") == (80.0, 120.0)
    assert limit_prices(100, "430047") == (70.0, 130.0)
    assert limit_prices(100, "600519", is_st=True) == (95.0, 105.0)
    assert limit_prices(100, "600519", new_listing=True) == (None, None)


def test_can_buy_and_sell_limits():
    rules = ExecutionRules()
    ok, reason = can_buy(row=_row(109.99), prev_close=100.0, code="600519", rules=rules)
    assert ok and not reason
    blocked, reason = can_buy(row=_row(110.0), prev_close=100.0, code="600519", rules=rules)
    assert not blocked and reason == "limit_up"

    blocked, reason = can_sell(row=_row(90.0), prev_close=100.0, code="600519", rules=rules)
    assert not blocked and reason == "limit_down"
    ok, reason = can_sell(row=_row(90.01), prev_close=100.0, code="600519", rules=rules)
    assert ok and not reason


def test_suspension_blocks_trading():
    rules = ExecutionRules()
    ok, reason = can_buy(row=_row(10.0, volume=0), prev_close=10.0, code="600519", rules=rules)
    assert not ok and reason == "suspended"


def test_t_plus_one_available_shares():
    ledger = TPlusOneLedger(enabled=True)
    day1 = pd.Timestamp("2024-01-02").date()
    day2 = pd.Timestamp("2024-01-03").date()
    ledger.start_day(day1)
    ledger.on_buy("600519", 100)
    assert ledger.available("600519") == 0
    ledger.start_day(day2)
    assert ledger.available("600519") == 100
    ledger.on_sell("600519", 40)
    assert ledger.available("600519") == 60


def test_fee_model_stamp_tax_schedule():
    rules = ExecutionRules()
    model = FeeModel(rules)
    assert model.stamp_rate("2023-08-27") == pytest.approx(0.001)
    assert model.stamp_rate("2023-08-28") == pytest.approx(0.0005)
    buy_fee = model.fee(shares=1000, price=10.0, side="buy", dt="2024-01-02")
    sell_fee = model.fee(shares=1000, price=10.0, side="sell", dt="2024-01-02")
    assert buy_fee == pytest.approx(5.1)
    assert sell_fee == pytest.approx(10.1)
