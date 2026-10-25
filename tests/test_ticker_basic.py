# 该用例依赖真实网络/QuestDB：默认跳过，RUN_LIVE_TESTS=1 pytest 可开启
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1",
    reason="需要真实数据源/QuestDB；设置 RUN_LIVE_TESTS=1 开启",
)
import sys
import os

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from data_interface.market.loader import MarketDataLoader


def test_ticker_basic_info():
    loader = MarketDataLoader()
    # Use a sample stock code
    stock_code = "002977"
    print(f"Fetching basic info for {stock_code}...")

    try:
        data = loader.ticker_basic_info(stock_code)
        if not data.empty:
            print("Successfully fetched ticker basic info:")
            print(data)
            # Print columns to verify structure
            print("\nColumns:", data.columns.tolist())
        else:
            print("No data found or empty DataFrame returned.")
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_ticker_basic_info()

