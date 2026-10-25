"""持仓接口层（面向 LLM）的离线集成测试。

覆盖：资金扣减、卖出校验、action 白名单、资金更新失败回滚、
``pm_modify_position(quantity=...)`` 的持久性、响应信封。
"""

from __future__ import annotations

import pytest


def _new_portfolio(interface, cash: float = 10000.0) -> str:
    response = interface.create_portfolio("Test", cash)
    assert response["success"], response
    return response["data"]["portfolio_id"]


def test_create_portfolio_response_envelope(position_interface):
    response = position_interface.create_portfolio("Alpha", 50000.0)
    assert response["success"] is True
    assert "data" in response and "portfolio_id" in response["data"]
    assert "error_details" not in response


def test_buy_then_sell_updates_cash_and_pnl(position_interface):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    opened = position_interface.open_position(portfolio_id, "600519")
    assert opened["success"], opened

    buy = position_interface.execute_trade(portfolio_id, "600519", "Buy", 200, 10.0)
    assert buy["success"], buy
    assert buy["data"]["cash_balance"] == pytest.approx(8000.0)
    assert buy["data"]["new_quantity"] == pytest.approx(200)

    sell = position_interface.execute_trade(portfolio_id, "600519", "sell", 200, 12.0)
    assert sell["success"], sell
    assert sell["data"]["cash_balance"] == pytest.approx(10400.0)

    status = position_interface.get_portfolio_status(portfolio_id)
    assert status["success"]
    assert status["data"]["cash"] == pytest.approx(10400.0)
    assert status["data"]["positions"][0]["quantity"] == pytest.approx(0)


def test_oversell_is_rejected_but_trade_not_written(position_interface):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    position_interface.open_position(portfolio_id, "600519")
    position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)

    response = position_interface.execute_trade(portfolio_id, "600519", "sell", 500, 11.0)
    assert response["success"] is False
    assert response["error_details"]["type"] == "LedgerError"

    status = position_interface.get_portfolio_status(portfolio_id)
    assert status["data"]["positions"][0]["quantity"] == pytest.approx(100)
    assert status["data"]["cash"] == pytest.approx(9000.0)


@pytest.mark.parametrize("action", ["open", "sel", "shbuy"])
def test_unknown_action_is_not_treated_as_sell(position_interface, action):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    position_interface.open_position(portfolio_id, "600519")
    position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)

    response = position_interface.execute_trade(portfolio_id, "600519", action, 100, 10.0)
    assert response["success"] is False
    assert response["error_details"]["type"] == "InvalidAction"
    status = position_interface.get_portfolio_status(portfolio_id)
    assert status["data"]["cash"] == pytest.approx(9000.0)


def test_insufficient_cash(position_interface):
    portfolio_id = _new_portfolio(position_interface, 100.0)
    position_interface.open_position(portfolio_id, "600519")
    response = position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)
    assert response["success"] is False
    assert response["error_details"]["type"] == "InsufficientCash"


def test_cash_update_failure_rolls_back_trade(position_interface, monkeypatch):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    position_interface.open_position(portfolio_id, "600519")
    monkeypatch.setattr(position_interface.manager, "modify_profolio", lambda *a, **k: False)

    response = position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)
    assert response["success"] is False
    assert response["error_details"]["type"] == "DatabaseError"

    position = position_interface.manager.get_position_by_ticker(portfolio_id, "600519")
    assert position_interface.manager.list_trade_record(position.pos_id) == []


def test_modify_quantity_is_durable(position_interface):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    opened = position_interface.open_position(portfolio_id, "600519")
    pos_id = opened["data"]["pos_id"]
    position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)

    response = position_interface.modify_position(pos_id, quantity=60)
    assert response["success"], response

    status = position_interface.get_portfolio_status(portfolio_id)
    assert status["data"]["positions"][0]["quantity"] == pytest.approx(60)


def test_close_position_uses_ledger_quantity(position_interface):
    portfolio_id = _new_portfolio(position_interface, 10000.0)
    position_interface.open_position(portfolio_id, "600519")
    position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)

    response = position_interface.close_position(portfolio_id, "600519", 11.0)
    assert response["success"], response
    assert response["data"]["new_quantity"] == pytest.approx(0)
    status = position_interface.get_portfolio_status(portfolio_id)
    # 10000 - 100*10 + 100*11 = 10100
    assert status["data"]["cash"] == pytest.approx(10100.0)


def test_signals_use_real_pyramid_level(position_interface):
    portfolio_id = _new_portfolio(position_interface, 100000.0)
    opened = position_interface.open_position(
        portfolio_id, "600519", mode="ATR", n_factor=2.0, risk_per_trade=0.02
    )
    pos_id = opened["data"]["pos_id"]
    position_interface.execute_trade(portfolio_id, "600519", "buy", 100, 10.0)
    # 模拟已经加过一次仓
    position_interface.manager.modify_position(
        pos_id, {"mode_params": {"pyramid_level": 1}}
    )
    response = position_interface.get_trading_signals(portfolio_id, "600519")
    assert response["success"], response
    assert response["data"]["pyramid_level"] == 1
