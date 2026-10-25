"""持仓管理器的离线集成测试（SQLite + 行情桩）。

覆盖：组合/仓位 CRUD、成交入账、卖出校验、数量调整的持久性、级联软删除。
"""

from __future__ import annotations

import pytest

from position.manager import BUY, SELL, PositionError, compute_ledger


def test_portfolio_create_and_modify_in_place(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("Test Portfolio", cash=10000.0)
    assert portfolio_id

    assert sqlite_manager.modify_profolio(
        portfolio_id, {"cash": 15000.0, "name": "Updated"}
    )

    portfolios = sqlite_manager.list__profolio()
    assert len(portfolios) == 1
    assert portfolios[0].cash == pytest.approx(15000.0)
    assert portfolios[0].name == "Updated"

    # 同一组合只应有一条记录（UPDATE 原地更新，不再追加历史行）
    session = sqlite_manager.Session()
    all_rows = session.query(type(portfolios[0])).all()
    session.close()
    assert len(all_rows) == 1


def test_create_position_and_list(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=100000.0)
    pos_id = sqlite_manager.create_position(
        portfolio_id, "600519", "贵州茅台", "ATR", {"n_factor": 2.0}
    )
    assert pos_id
    positions = sqlite_manager.list_positions(portfolio_id)
    assert len(positions) == 1
    assert positions[0].ticker == "600519"
    assert positions[0].mode == "ATR"
    assert positions[0].mode_instance is not None


def test_buy_updates_quantity_cost_and_market_value(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")

    trade_id = sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)
    assert trade_id

    position = sqlite_manager.list_positions(portfolio_id)[0]
    assert position.quantity == pytest.approx(100)
    assert position.cost == pytest.approx(10.0)
    # 行情桩最新价 10.0 -> 市值 1000
    assert position.market_value == pytest.approx(1000.0)
    assert position.unrealized_profit == pytest.approx(0.0)


def test_partial_sell_realizes_profit(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)
    sqlite_manager.add_trade_record(pos_id, SELL, 40, 12.0)

    position = sqlite_manager.list_positions(portfolio_id)[0]
    assert position.quantity == pytest.approx(60)
    assert position.cost == pytest.approx(10.0)
    assert position.realized_profit == pytest.approx(80.0)


def test_sell_more_than_holdings_is_rejected(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)

    with pytest.raises(PositionError):
        sqlite_manager.add_trade_record(pos_id, SELL, 150, 12.0)

    position = sqlite_manager.list_positions(portfolio_id)[0]
    assert position.quantity == pytest.approx(100)  # 未被污染
    trades = sqlite_manager.list_trade_record(pos_id)
    assert len(trades) == 1


def test_reentry_after_full_exit_uses_new_cost(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=100000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)
    sqlite_manager.add_trade_record(pos_id, SELL, 100, 20.0)
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 30.0)

    trades = sqlite_manager.list_trade_record(pos_id)
    state = compute_ledger(trades)
    assert state.average_cost == pytest.approx(30.0)
    assert state.realized_profit == pytest.approx(1000.0)


def test_adjusted_quantity_survives_recalculation(sqlite_manager):
    """旧实现直接改 quantity 字段，下一次 calculate_profit 会被流水覆盖回原值。"""
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)

    assert sqlite_manager.adjust_quantity(pos_id, 60)
    sqlite_manager.calculate_profit(pos_id)

    position = sqlite_manager.list_positions(portfolio_id)[0]
    assert position.quantity == pytest.approx(60)
    types = [trade.type for trade in sqlite_manager.list_trade_record(pos_id)]
    assert "调整" in types


def test_delete_position_soft_deletes_trades(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    sqlite_manager.add_trade_record(pos_id, BUY, 100, 10.0)

    assert sqlite_manager.delete_position(pos_id)
    assert sqlite_manager.list_positions(portfolio_id) == []
    assert sqlite_manager.list_trade_record(pos_id) == []


def test_invalid_trade_arguments(sqlite_manager):
    portfolio_id = sqlite_manager.create_profolio("P", cash=10000.0)
    pos_id = sqlite_manager.create_position(portfolio_id, "000001", "平安银行", "ATR")
    with pytest.raises(PositionError):
        sqlite_manager.add_trade_record(pos_id, BUY, 0, 10.0)
    with pytest.raises(PositionError):
        sqlite_manager.add_trade_record(pos_id, BUY, 10, -1)
    with pytest.raises(PositionError):
        sqlite_manager.add_trade_record(pos_id, "开仓", 10, 10.0)
