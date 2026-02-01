import sys
import os
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.DataConnect import questDB
from sqlalchemy import text

# Setup logging - only show errors and above for this module
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("InitDB")


def init_tables():
    """Initialize tables - idempotent operation (safe to call multiple times)"""
    try:
        db = questDB()
        engine = db.pd_conn

        # Define tables
        tables = {
            "portfolios": """
                CREATE TABLE IF NOT EXISTS portfolios (
                    id TIMESTAMP,
                    portfolio_id SYMBOL,
                    name STRING,
                    total_assets DOUBLE,
                    cash DOUBLE,
                    market_value DOUBLE,
                    unrealized_profit DOUBLE,
                    realized_profit DOUBLE,
                    deleted_at TIMESTAMP
                ) timestamp(id) PARTITION BY YEAR WAL
            """,
            "positions": """
                CREATE TABLE IF NOT EXISTS positions (
                    id TIMESTAMP,
                    pos_id SYMBOL,
                    portfolio_id SYMBOL,
                    ticker SYMBOL,
                    name STRING,
                    mode SYMBOL,
                    created_at TIMESTAMP,
                    cost DOUBLE,
                    quantity DOUBLE,
                    market_value DOUBLE,
                    unrealized_profit DOUBLE,
                    unrealized_profit_rate DOUBLE,
                    realized_profit DOUBLE,
                    total_profit DOUBLE,
                    updated_at TIMESTAMP,
                    mode_params STRING,
                    deleted_at TIMESTAMP,
                    stop_price DOUBLE,
                    first_entry_date TIMESTAMP
                ) timestamp(id) PARTITION BY YEAR WAL
            """,
            "trade_records": """
                CREATE TABLE IF NOT EXISTS trade_records (
                    trade_id SYMBOL,
                    pos_id SYMBOL,
                    type SYMBOL,
                    quantity DOUBLE,
                    price DOUBLE,
                    trade_date TIMESTAMP,
                    updated_at TIMESTAMP,
                    deleted_at TIMESTAMP
                ) timestamp(trade_date) PARTITION BY YEAR WAL
            """,
        }

        with engine.connect() as conn:
            for name, sql in tables.items():
                try:
                    conn.execute(text(sql))
                except Exception:
                    pass  # Table may already exist

            conn.commit()

        # Add any missing columns to existing tables (silent)
        _add_missing_columns(db)

    except Exception:
        pass  # QuestDB not running or other error


def _add_missing_columns(db):
    """Add missing columns to existing tables silently"""
    columns = [
        ("positions", "stop_price", "DOUBLE"),
        ("positions", "first_entry_date", "TIMESTAMP"),
    ]

    for table, column, col_type in columns:
        try:
            # Check if column exists
            with db.pd_conn.connect() as conn:
                conn.execute(text(f'SELECT "{column}" FROM "{table}" LIMIT 0'))
        except Exception:
            # Column doesn't exist, add it
            try:
                db.execute_raw_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
            except Exception:
                pass  # Column may already exist or added by another process


if __name__ == "__main__":
    init_tables()
