import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
from datetime import datetime
import sys
import os
import json

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position import PositionAgentInterface
from position.schema import Base, PositionModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class TestLLMInterface(unittest.TestCase):
    def setUp(self):
        # Setup In-Memory DB
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # Patch QuestDB in manager
        self.patcher = patch("position.manager.questDB")
        self.mock_quest_db_cls = self.patcher.start()
        self.mock_quest_db = self.mock_quest_db_cls.return_value
        self.mock_quest_db.pd_conn = self.engine

        # Mock DataLoader
        self.mock_loader = MagicMock()

        # Initialize Interface
        self.interface = PositionAgentInterface(data_loader=self.mock_loader)
        # Override session in manager
        self.interface.manager.Session = self.Session

    def tearDown(self):
        self.patcher.stop()
        Base.metadata.drop_all(self.engine)

    def test_workflow(self):
        print("\nTesting LLM Interface Workflow...")

        # 1. Create Portfolio
        resp = self.interface.create_portfolio("AI Fund", 100000.0)
        self.assertTrue(resp["success"])
        pid = resp["data"]["portfolio_id"]

        # 2. Get Buying Advice (Mock Data)
        # Setup Mock for OHLC
        mock_df = pd.DataFrame(
            {
                "close": [100.0, 101.0, 102.0],
                "high": [105.0, 105.0, 105.0],
                "low": [95.0, 95.0, 95.0],
                "volume": [1000, 1000, 1000],
            },
            index=pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
        )
        # Mock the return of data_loader.ohlc
        self.mock_loader.ohlc.return_value = {"AAPL": mock_df}

        # Patch IndicatorEngine to avoid complex calculation logic dependencies
        with patch("position.modes.atr_mode.IndicatorEngine") as MockEngine:
            mock_engine_instance = MockEngine.return_value
            mock_engine_instance.calculate.return_value = pd.DataFrame(
                {"atr": [5.0]}, index=[pd.Timestamp("2023-01-03")]
            )

            advice = self.interface.get_buying_advice(
                pid, "AAPL", initial_risk_per_trade=0.02
            )
            self.assertTrue(advice["success"])
            # Risk = 100000 * 0.02 = 2000.
            # ATR = 5.0. D = 2*5 = 10.
            # Qty = 2000 / 10 = 200.
            self.assertEqual(advice["data"]["quantity"], 200)

        # 3. Open Position
        # Explicitly pass initial_atr so signals can work later
        open_resp = self.interface.open_position(
            pid, "AAPL", "Apple Inc", "ATR", initial_atr=5.0
        )
        self.assertTrue(open_resp["success"])

        # 4. Execute Trade (Buy)
        # 200 shares @ 102.0
        trade_resp = self.interface.execute_trade(pid, "AAPL", "Buy", 200, 102.0)
        self.assertTrue(trade_resp["success"])
        self.assertEqual(trade_resp["data"]["new_quantity"], 200.0)

        # Verify Quantity in Summary
        summary = self.interface.get_portfolio_status(pid)
        self.assertTrue(summary["success"])
        pos_data = summary["data"]["positions"][0]
        self.assertEqual(pos_data["ticker"], "AAPL")
        self.assertEqual(pos_data["quantity"], 200.0)
        self.assertEqual(pos_data["market_value"], 20400.0)  # 200 * 102

        # 5. Check Signals (Price moves up)
        # New Price 110.0
        mock_df_new = pd.DataFrame(
            {"close": [110.0], "high": [112.0], "low": [108.0], "volume": [1000]},
            index=pd.to_datetime(["2023-01-04"]),
        )
        self.mock_loader.ohlc.return_value = {"AAPL": mock_df_new}

        # We need to ensure the mode instance has the correct 'stop_price' from creation
        # Open position sets stop_price? No, it defaults to 0.0 in base init.
        # But calculate_purchase_advice returns stop_price.
        # Usually we should update position with stop price after buy.
        # In current logic, open_position doesn't set stop_price unless passed?
        # ATRMode __init__ takes stop_price.
        # But PositionModel mode_params stores it.
        # If we didn't pass it to open_position, it is 0.

        # Let's see if we can update it via signals logic or manual update.
        # In a real flow, Agent would set stop price via 'pm_update_position' (which we removed from server but interface has open_position kwargs).
        # Actually, execute_trade logic just adds trade.
        # We might need to manually set the stop price for the test to be realistic about signals.
        # But let's just check if it runs without error first.

        signals = self.interface.get_trading_signals(pid, "AAPL")
        self.assertTrue(signals["success"])
        self.assertEqual(signals["data"]["current_price"], 110.0)

        # 6. Execute Trade (Sell/Close)
        close_resp = self.interface.close_position(pid, "AAPL", 110.0)
        self.assertTrue(close_resp["success"])

        summary_final = self.interface.get_portfolio_status(pid)
        self.assertEqual(summary_final["data"]["positions"][0]["quantity"], 0.0)
        # Cash should be 100000 - 20400 + 22000 = 101600.
        self.assertEqual(summary_final["data"]["cash"], 101600.0)


if __name__ == "__main__":
    unittest.main()
