import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import pandas as pd
from datetime import datetime, timedelta
import uuid
import time

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position.manager import PositionManager
from scripts.init_position_db import init_tables
from utils.DataConnect import questDB


class TestPositionQuestDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """确保 QuestDB 表已初始化 (如果不存在)"""
        print("\nEnsuring QuestDB tables exist...")
        init_tables()
        cls.db = questDB()

    def setUp(self):
        """测试准备：使用前缀生成唯一的测试数据"""
        self.test_prefix = f"TEST_{str(uuid.uuid4())[:8]}_"
        self.mock_loader = MagicMock()
        self.manager = PositionManager(data_loader=self.mock_loader)
        self.created_portfolios = []

    def tearDown(self):
        """测试清理：删除所有测试创建的组合"""
        print(f"\nCleaning up {len(self.created_portfolios)} test portfolios...")
        for pid in self.created_portfolios:
            try:
                self.manager.delete_profolio(pid)
            except Exception as e:
                print(f"Failed to cleanup portfolio {pid}: {e}")

    def test_profolio_lifecycle(self):
        """测试投资组合的生命周期：创建、列表、重名检查、删除"""
        print("\nTesting Profolio Lifecycle...")
        name = self.test_prefix + "Main"

        # 1. Create
        pid = self.manager.create_profolio(name, cash=100000.0)
        self.assertIsNotNone(pid)
        self.created_portfolios.append(pid)

        # Wait for QuestDB WAL to commit
        time.sleep(0.6)

        # 2. List & Verify
        profolios = self.manager.list__profolio()
        found = next((p for p in profolios if p.portfolio_id == pid), None)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, name)
        self.assertEqual(found.cash, 100000.0)

        # 3. Duplicate Prevention
        pid_again = self.manager.create_profolio(name, cash=500.0)
        self.assertEqual(pid, pid_again)  # Should return existing ID

    def test_position_and_trades(self):
        """测试持仓和交易逻辑：建仓、交易、盈亏计算、数量同步"""
        print("\nTesting Position and Trades...")
        pid = self.manager.create_profolio(self.test_prefix + "Trading", cash=100000.0)
        self.created_portfolios.append(pid)

        # Wait for WAL commit
        time.sleep(0.6)

        # 1. Create Position
        ticker = "AAPL"
        pos_id = self.manager.create_position(
            pid, ticker, "Apple Inc", "ATR", {"initial_atr": 5.0}
        )
        self.assertIsNotNone(pos_id)

        # 2. Add Buy Trade
        # Buy 100 @ 150
        t1 = datetime.utcnow() - timedelta(minutes=10)
        self.manager.add_trade_record(pos_id, "买入", 100, 150.0, trade_date=t1)

        # Wait for WAL commit
        time.sleep(0.6)

        # Verify Stats
        pos = self.manager.get_position_by_ticker(pid, ticker)
        self.assertEqual(pos.quantity, 100.0)
        self.assertEqual(pos.cost, 150.0)

        # 3. Add Sell Trade (Partial)
        # Sell 40 @ 160. Profit = (160-150)*40 = 400.
        t2 = datetime.utcnow() - timedelta(minutes=5)
        self.manager.add_trade_record(pos_id, "卖出", 40, 160.0, trade_date=t2)

        # Wait for WAL commit
        time.sleep(0.6)

        pos = self.manager.get_position_by_ticker(pid, ticker)
        self.assertEqual(pos.quantity, 60.0)
        self.assertEqual(pos.realized_profit, 400.0)

    def test_portfolio_summary_and_refresh(self):
        """测试全景汇总和实时刷新逻辑"""
        print("\nTesting Portfolio Summary and Refresh...")
        pid = self.manager.create_profolio(self.test_prefix + "Summary", cash=50000.0)
        self.created_portfolios.append(pid)

        # Wait for WAL commit
        time.sleep(0.6)

        # Open position
        ticker = "TSLA"
        pos_id = self.manager.create_position(
            pid, ticker, "Tesla", "ATR", {"initial_atr": 10.0}
        )
        self.manager.add_trade_record(pos_id, "买入", 10, 200.0)  # Cost 2000

        # Wait for WAL commit
        time.sleep(0.6)

        # Mock DataLoader for "real-time" price
        # DataLoader.ohlc returns {ticker: df}
        mock_df = pd.DataFrame({"close": [210.0]}, index=[pd.Timestamp.now()])
        self.mock_loader.ohlc.return_value = {ticker: mock_df}

        # Refresh
        self.manager.refresh_all_positions(pid)

        # Wait for WAL commit
        time.sleep(0.6)

        # Check Summary
        summary = self.manager.get_portfolio_summary(pid)
        self.assertEqual(summary["portfolio_name"], self.test_prefix + "Summary")
        self.assertEqual(summary["market_value"], 2100.0)  # 10 * 210
        self.assertEqual(summary["total_unrealized_pnl"], 100.0)  # (210-200)*10

    def test_delete_cascading(self):
        """测试级联删除：删除组合应清除持仓和交易"""
        print("\nTesting Deletion Cascading...")
        pid = self.manager.create_profolio(self.test_prefix + "Delete", cash=1000.0)
        pos_id = self.manager.create_position(pid, "BTC", "Bitcoin", "ATR")
        self.manager.add_trade_record(pos_id, "买入", 1, 50000.0)

        # Wait for WAL commit
        time.sleep(0.6)

        # Perform Deletion
        success = self.manager.delete_profolio(pid)
        self.assertTrue(success)

        # Wait for WAL commit
        time.sleep(0.6)

        # Verify nothing remains
        profolios = self.manager.list__profolio()
        self.assertFalse(any(p.portfolio_id == pid for p in profolios))

        # Check positions (manual check via raw query or manager)
        # Note: manager doesn't have a global list_all_positions, so we trust the CASCADE log or use raw DB check
        with self.db.pd_conn.connect() as conn:
            from sqlalchemy import text

            res = conn.execute(
                text(
                    "SELECT count(*) FROM positions WHERE portfolio_id = :pid AND deleted_at IS NULL"
                ),
                {"pid": pid},
            ).fetchone()
            self.assertEqual(res[0], 0)
            res_t = conn.execute(
                text(
                    "SELECT count(*) FROM trade_records WHERE pos_id = :pid AND deleted_at IS NULL"
                ),
                {"pid": pos_id},
            ).fetchone()
            self.assertEqual(res_t[0], 0)


if __name__ == "__main__":
    unittest.main()
