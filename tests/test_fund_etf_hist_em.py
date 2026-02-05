import sys
import os
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

import akshare as ak

etf_code = "518880"
start_date = "20250101"
end_date = "20260205"

if __name__ == "__main__":
    print(f"Testing ak.fund_etf_hist_em with ETF code: {etf_code}")
    print(f"Date range: {start_date} to {end_date}")
    print("-" * 50)

    # Try Eastmoney first, fallback to Sina if connection fails
    try:
        print("Attempting to fetch data from Eastmoney...")
        df = ak.fund_etf_hist_em(
            symbol=etf_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
    except Exception as e:
        print(f"Eastmoney fetch failed: {e}")
        print("Falling back to Sina...")
        # Sina symbol needs sz/sh prefix
        sina_symbol = (
            f"sz{etf_code}"
            if etf_code.startswith(("15", "16", "18", "39", "159"))
            else f"sh{etf_code}"
        )
        df = ak.fund_etf_hist_sina(symbol=sina_symbol)

        # Filter dates for Sina
        from datetime import datetime

        start_dt = datetime.strptime(start_date, "%Y%m%d").date()
        end_dt = datetime.strptime(end_date, "%Y%m%d").date()
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]

        # Rename columns to match EM format if necessary, though EM has more columns
        # For this test, we just want to see the data.

    print(f"Columns: {df.columns.tolist()}")
    print(f"Shape: {df.shape}")
    print("-" * 50)
    print("First 5 rows:")
    print(df.head())
    print("-" * 50)
    print("Last 5 rows:")
    print(df.tail())
