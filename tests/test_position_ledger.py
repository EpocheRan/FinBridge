"""账本推算（移动加权平均成本）与交易参数校验的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from position.manager import compute_ledger
from position.llm_tool import PositionAgentInterface


def trade(trade_type: str, quantity: float, price: float):
    return SimpleNamespace(type=trade_type, quantity=quantity, price=price)


def test_simple_buy():
    state = compute_ledger([trade("买入", 100, 10.0)])
    assert state.quantity == pytest.approx(100)
    assert state.average_cost == pytest.approx(10.0)
    assert state.realized_profit == pytest.approx(0.0)


def test_partial_sell_realizes_profit_and_keeps_cost():
    state = compute_ledger([trade("买入", 100, 10.0), trade("卖出", 40, 12.0)])
    assert state.quantity == pytest.approx(60)
    assert state.average_cost == pytest.approx(10.0)
    assert state.realized_profit == pytest.approx(80.0)


def test_average_cost_blends_adds():
    state = compute_ledger([trade("买入", 100, 10.0), trade("买入", 100, 20.0)])
    assert state.average_cost == pytest.approx(15.0)


def test_reentry_after_full_exit_uses_new_cost():
    """旧实现把已卖出部分的买入额也算进均价（10 买、20 卖、30 再买 -> 成本 20）。"""
    state = compute_ledger(
        [trade("买入", 100, 10.0), trade("卖出", 100, 20.0), trade("买入", 100, 30.0)]
    )
    assert state.quantity == pytest.approx(100)
    assert state.average_cost == pytest.approx(30.0)
    assert state.realized_profit == pytest.approx(1000.0)


def test_adjust_only_moves_quantity():
    state = compute_ledger([trade("买入", 100, 10.0), trade("调整", -30, 10.0)])
    assert state.quantity == pytest.approx(70)
    assert state.average_cost == pytest.approx(10.0)
    assert state.realized_profit == pytest.approx(0.0)


def test_adjust_to_zero_clears_cost():
    state = compute_ledger(
        [trade("买入", 100, 10.0), trade("调整", -100, 10.0), trade("买入", 50, 40.0)]
    )
    assert state.quantity == pytest.approx(50)
    assert state.average_cost == pytest.approx(40.0)


def test_sell_more_than_held_is_clamped_in_ledger():
    state = compute_ledger([trade("买入", 10, 10.0), trade("卖出", 50, 12.0)])
    assert state.quantity == 0


@pytest.mark.parametrize(
    "action,expected",
    [
        ("buy", "买入"),
        ("Buy", "买入"),
        ("买入", "买入"),
        ("sell", "卖出"),
        ("close", "卖出"),
        ("平仓", "卖出"),
    ],
)
def test_action_whitelist_accepts_known_values(action, expected):
    assert PositionAgentInterface._normalize_action(action) == expected


@pytest.mark.parametrize("action", ["open", "sel", "shbuy", "", "做多"])
def test_action_whitelist_rejects_unknown_values(action):
    with pytest.raises(ValueError):
        PositionAgentInterface._normalize_action(action)
