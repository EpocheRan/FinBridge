import sys
import os
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.DataConnect import questDB
from sqlalchemy import text

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InitDB")


def init_tables():
    logger.info("Initializing tables manually for QuestDB (WAL enabled)...")

    try:
        db = questDB()
        engine = db.pd_conn

        sqls = [
            """
            CREATE TABLE IF NOT EXISTS profolios (
                id TIMESTAMP,
                profolio_id SYMBOL,
                name STRING,
                total_assets DOUBLE,
                cash DOUBLE,
                market_value DOUBLE,
                unrealized_profit DOUBLE,
                realized_profit DOUBLE
            ) timestamp(id) PARTITION BY YEAR WAL;
            """,
            """
            CREATE TABLE IF NOT EXISTS positions (
                id TIMESTAMP,
                pos_id SYMBOL,
                profolio_id SYMBOL,
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
                mode_params STRING
            ) timestamp(id) PARTITION BY YEAR WAL;
            """,
            """
            CREATE TABLE IF NOT EXISTS trade_records (
                trade_id SYMBOL,
                pos_id SYMBOL,
                type SYMBOL,
                quantity DOUBLE,
                price DOUBLE,
                trade_date TIMESTAMP,
                updated_at TIMESTAMP
            ) timestamp(trade_date) PARTITION BY YEAR WAL;
            """,
        ]

        with engine.connect() as conn:
            for s in sqls:
                try:
                    conn.execute(text(s))
                    logger.info("Executed create table.")
                except Exception as e:
                    logger.warning(f"Error executing SQL: {e}")
            conn.commit()

        logger.info("Manual table creation attempt finished.")

    except Exception as e:
        logger.error(f"Failed to connect or execute: {e}")


if __name__ == "__main__":
    init_tables()
