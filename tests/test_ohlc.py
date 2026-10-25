# 该用例依赖真实网络/QuestDB：默认跳过，RUN_LIVE_TESTS=1 pytest 可开启
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1",
    reason="需要真实数据源/QuestDB；设置 RUN_LIVE_TESTS=1 开启",
)
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from data_interface.market.loader import MarketDataLoader

stock_codes = "603806"
start_date = "2025-01-01"
end_date = "2026-02-05"

if __name__ == "__main__":
    data_loader = MarketDataLoader()
    stock_data = data_loader.ohlc(stock_codes, start_date, end_date, "d", now=False)
    print(stock_data[stock_codes])

