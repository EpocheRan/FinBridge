import sys
import os
import pandas as pd

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

