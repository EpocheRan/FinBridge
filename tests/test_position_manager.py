import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import pandas as pd
from datetime import datetime, timedelta
import json

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position.manager import PositionManager
from position.schema import Base, PortfolioModel, PositionModel, TradeRecordModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


class TestPositionManager(unittest.TestCase):
    def setUp(self):
        # Use in-memory SQLite for testing
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # Patch the PositionManager to use our test engine
        self.patcher = patch("position.manager.QuestDBClient")
        self.mock_quest_db_cls = self.patcher.start()
        self.mock_quest_db = self.mock_quest_db_cls.return_value
        self.mock_quest_db.pd_conn = self.engine

        # Mock DataLoader
        self.mock_loader = MagicMock()

        self.manager = PositionManager(data_loader=self.mock_loader)
        # Override the session factory in manager to use our engine
        self.manager.Session = self.Session

    def tearDown(self):
        self.patcher.stop()
        Base.metadata.drop_all(self.engine)

    def test_profolio_crud(self):
        """Test Profolio Create, Modify"""
        print("\nTesting Profolio CRUD...")
        # Create
        pid = self.manager.create_profolio("Test Portfolio", cash=10000.0)
        self.assertIsNotNone(pid)

        # Verify Create
        profolios = self.manager.list__profolio()
        self.assertEqual(len(profolios), 1)
        self.assertEqual(profolios[0].name, "Test Portfolio")
        self.assertEqual(profolios[0].cash, 10000.0)

        # Modify
        success = self.manager.modify_profolio(
            pid, {"cash": 15000.0, "name": "Updated Portfolio"}
        )
        self.assertTrue(success)

        # Verify Modify (History check)
        session = self.Session()
        all_records = session.query(PortfolioModel).filter_by(portfolio_id=pid).all()
        self.assertEqual(len(all_records), 2)  # Original + Modified

        latest = self.manager.list__profolio()[0]
        self.assertEqual(latest.cash, 15000.0)
        self.assertEqual(latest.name, "Updated Portfolio")
        session.close()

    def test_position_crud(self):
        """Test Position Create, Modify, Delete"""
        print("\nTesting Position CRUD...")
        pid = self.manager.create_profolio("Test Portfolio", cash=100000.0)

        # Create Position with ATR Mode
        mode_params = {
            "initial_atr": 1.0,
            "n_factor": 2.0,
            "time_stop_days": 5,
            "risk_per_trade": 0.02,
        }
        pos_id = self.manager.create_position(
            pid, "AAPL", "Apple Inc.", "ATR", mode_params
        )
        self.assertIsNotNone(pos_id)

        # Verify Create
        positions = self.manager.list_positions(pid)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticker, "AAPL")
        self.assertEqual(positions[0].mode, "ATR")

        # Check if mode instance is correctly attached
        self.assertTrue(hasattr(positions[0], "mode_instance"))
        self.assertEqual(positions[0].mode_instance.initial_atr, 1.0)

        # Modify Position
        success = self.manager.modify_position(pos_id, {"market_value": 500.0})
        self.assertTrue(success)

        # Verify Modify
        latest_pos = self.manager.list_positions(pid)[0]
        self.assertEqual(latest_pos.market_value, 500.0)

        # Delete Position
        success = self.manager.delete_position(pos_id)
        self.assertTrue(success)

        positions = self.manager.list_positions(pid)
        self.assertEqual(len(positions), 0)

    def test_trade_crud_and_calculation(self):
        """Test Trade Add, Modify, Delete and Profit Calculation"""
        print("\nTesting Trade CRUD and Profit Calculation...")
        pid = self.manager.create_profolio("Trade Portfolio", cash=10000.0)
        pos_id = self.manager.create_position(pid, "TSLA", "Tesla", "ATR")

        # 1. Add Buy Trade
        # Ensure time difference
        t1 = datetime(2023, 1, 1, 12, 0, 0)
        trade_id1 = self.manager.add_trade_record(
            pos_id, "买入", 10, 100.0, trade_date=t1
        )  # Cost 1000
        self.assertIsNotNone(trade_id1)

        # Verify Profit Calculation
        pos = self.manager.list_positions(pid)[0]
        self.assertEqual(pos.cost, 100.0)
        self.assertEqual(pos.market_value, 1000.0)  # 10 * 100
        self.assertEqual(pos.total_profit, 0.0)

        # 2. Add Sell Trade (Partial)
        t2 = datetime(2023, 1, 2, 12, 0, 0)
        trade_id2 = self.manager.add_trade_record(
            pos_id, "卖出", 5, 120.0, trade_date=t2
        )  # Proceeds 600, Profit (120-100)*5 = 100

        # Verify Profit

        pos = self.manager.list_positions(pid)[0]
        self.assertEqual(pos.realized_profit, 100.0)
        # Remaining: 5 shares. Cost 100. Latest Price 120 (from last trade).
        # Market Value: 5 * 120 = 600
        # Unrealized: (120 - 100) * 5 = 100
        self.assertEqual(pos.market_value, 600.0)
        self.assertEqual(pos.unrealized_profit, 100.0)
        self.assertEqual(pos.total_profit, 200.0)

        # 3. Modify Trade
        # Change Buy price to 90.0.
        # New Avg Cost = 90.0.
        # Sell Profit = (120 - 90) * 5 = 150.
        # Unrealized = (120 - 90) * 5 = 150.
        # Total = 300.
        self.manager.modify_trade_record(trade_id1, {"price": 90.0})

        pos = self.manager.list_positions(pid)[0]
        self.assertEqual(pos.cost, 90.0)
        self.assertEqual(pos.realized_profit, 150.0)
        self.assertEqual(pos.unrealized_profit, 150.0)
        self.assertEqual(pos.total_profit, 300.0)

        # 4. Delete Trade
        # Delete the sell trade.
        # Back to 10 shares at 90.0. Latest price from last trade... wait, no last trade if we delete the sell?
        # If we delete the sell trade, only the buy trade remains. Latest price logic falls back to data_loader or last trade.
        # Here last trade is the buy trade at 90.0.
        self.manager.delete_trade_record(trade_id2)

        pos = self.manager.list_positions(pid)[0]
        self.assertEqual(pos.realized_profit, 0.0)
        self.assertEqual(pos.market_value, 900.0)  # 10 * 90
        self.assertEqual(pos.unrealized_profit, 0.0)

    @patch("position.modes.atr_mode.IndicatorEngine")
    @patch("position.modes.atr_mode.ATR")
    def test_atr_mode_functions(self, mock_atr_cls, mock_engine_cls):
        """Test ATR Mode Functions"""
        print("\nTesting ATR Mode Functions...")

        # Configure Mock Engine
        mock_engine_instance = mock_engine_cls.return_value
        # Mock calculate return value (ATR = 5.0)
        mock_engine_instance.calculate.return_value = pd.DataFrame(
            {"atr": [5.0]}, index=[pd.Timestamp("2023-01-03")]
        )

        pid = self.manager.create_profolio("Mode Portfolio", cash=100000.0)
        mode_params = {
            "initial_atr": 5.0,
            "n_factor": 2.0,  # D = 10.0
            "time_stop_days": 5,
            "risk_per_trade": 0.02,
        }
        pos_id = self.manager.create_position(pid, "NVDA", "Nvidia", "ATR", mode_params)
        pos = self.manager.list_positions(pid)[0]
        mode = pos.mode_instance

        # 1. calculate_stop_price
        # Hard Stop: 100 - 2*5 = 90
        # Structure Stop: 95
        # Max(90, 95) = 95
        stop_price = mode.calculate_stop_price(
            {"current_price": 100.0, "atr": 5.0, "structure_stop": 95.0}
        )
        self.assertEqual(stop_price, 95.0)

        # Test fallback if structure stop is low
        stop_price_atr = mode.calculate_stop_price(
            {"current_price": 100.0, "atr": 5.0, "structure_stop": 80.0}
        )
        self.assertEqual(stop_price_atr, 90.0)  # 100 - 10

        # Update mode stop price state for subsequent tests
        mode.stop_price = 90.0

        # 2. check_reduce_signal
        # a. Price Stop
        signal = mode.check_reduce_signal({"current_price": 89.0})
        self.assertEqual(signal, 1.0)  # Hit stop

        # b. Zombie Stop
        # Days held = 6 (>5), Profit = 0 (Entry 100, Cur 100), Threshold = 0.5 * 2 * 5 = 5.0
        signal_zombie = mode.check_reduce_signal(
            {
                "current_price": 100.0,
                "cost_price": 100.0,
                "days_held": 6,
                "has_added": False,
                "pyramid_level": 0,
            }
        )
        self.assertEqual(signal_zombie, 1.0)

        # c. No Signal
        signal_ok = mode.check_reduce_signal(
            {
                "current_price": 100.0,
                "cost_price": 100.0,
                "days_held": 2,  # < 5
            }
        )
        self.assertEqual(signal_ok, 0.0)

        # 3. check_add_signal
        # Initial P = 100. D = 10.
        # Level 0. Next trigger = 100 + (0+1)*0.5*10 = 105.

        # a. Not triggered
        signal_add_no = mode.check_add_signal(
            {
                "current_price": 104.0,
                "first_entry_price": 100.0,
                "initial_risk_distance": 10.0,
                "pyramid_level": 0,
            }
        )
        self.assertEqual(signal_add_no, {})

        # b. Triggered
        signal_add_yes = mode.check_add_signal(
            {
                "current_price": 105.5,
                "first_entry_price": 100.0,
                "initial_risk_distance": 10.0,
                "pyramid_level": 0,
                "base_quantity": 100,
            }
        )
        self.assertTrue(signal_add_yes["signal"])
        self.assertEqual(signal_add_yes["quantity"], 100)
        self.assertEqual(signal_add_yes["level"], 1)
        # New Stop for L1: P - 0.5D = 100 - 5 = 95.
        self.assertEqual(signal_add_yes["new_stop"], 95.0)

        # 4. calculate_purchase_advice
        # Total Capital = 100,000. Risk = 2% = 2000.
        # Price = 100. ATR = 5 (from mock). D = 10.
        # Qty = 2000 / 10 = 200.
        # Stop = 100 - 10 = 90.
        ohlcv = pd.DataFrame(
            {
                "date": [
                    datetime(2023, 1, 1),
                    datetime(2023, 1, 2),
                    datetime(2023, 1, 3),
                ],
                "close": [98, 99, 100],
                "high": [101, 101, 101],
                "low": [97, 97, 97],
            }
        )
        advice = mode.calculate_purchase_advice(
            {"ohlcv": ohlcv, "total_capital": 100000.0, "current_price": 100.0}
        )

        self.assertIn("quantity", advice)
        self.assertIn("stop_price", advice)
        print(f"Purchase Advice: {advice}")
        # Verify mocked value was used
        self.assertEqual(advice["initial_atr"], 5.0)

    def test_raw_db_verification(self):
        """Verify data exists in DB using raw connection"""
        print("\nTesting Raw DB Persistence...")
        pid = self.manager.create_profolio("Raw DB Test", cash=5555.0)

        # Open raw connection to the same engine
        with self.engine.connect() as conn:
            # Query profolios
            result = conn.execute(
                text("SELECT name, cash FROM portfolios WHERE portfolio_id = :pid"),
                {"pid": pid},
            )
            row = result.fetchone()
            print(f"Raw DB Fetch Profolio: {row}")
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Raw DB Test")
            self.assertEqual(row[1], 5555.0)

            # Create a position and query it
            pos_id = self.manager.create_position(
                pid, "DBTEST", "DB Test Ticker", "ATR"
            )
            result_pos = conn.execute(
                text("SELECT ticker, mode FROM positions WHERE pos_id = :pid"),
                {"pid": pos_id},
            )
            row_pos = result_pos.fetchone()
            print(f"Raw DB Fetch Position: {row_pos}")
            self.assertIsNotNone(row_pos)
            self.assertEqual(row_pos[0], "DBTEST")


if __name__ == "__main__":
    unittest.main()

