import backtrader as bt
from utils import DataLoader
from factorfactory import factor
import numpy as np
import pandas as pd

# 主程序
if __name__ == "__main__":
    loader = DataLoader()
    data = loader.ohlc("600378", "2024-01-01", "2025-06-10")
    print(data["600378"].head())