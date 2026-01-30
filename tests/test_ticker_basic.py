import sys
import os
import pandas as pd

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from utils.DataLoader import DataLoader


def test_ticker_basic_info():
    loader = DataLoader()
    # Use a sample stock code
    stock_code = "161129"
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
