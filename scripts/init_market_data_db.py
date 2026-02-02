import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.DataConnect import questDB

# Setup logging
logger = logging.getLogger("InitMarketDB")
logging.basicConfig(level=logging.INFO)


def get_ohlc_table_name(freq):
    """根据股票代码和频率生成表名 (Copy from questDB to avoid import loop if needed, but we imported questDB)"""
    return questDB.get_ohlc_table_name(freq)


def init_market_tables(db=None):
    """Initialize all market data tables."""
    if db is None:
        db = questDB()

    logger.info("Initializing market data tables...")

    _create_ohlc_table(db)
    _create_stock_basic_table(db)
    _create_fund_basic_table(db)
    _create_financial_indicators_table(db)
    _create_stock_industry_table(db)
    _create_rolling_financial_table(db)
    _create_concept_boards_table(db)
    _create_concept_ohlc_table(db)
    _create_concept_constituents_table(db)
    _create_trading_days_table(db)

    logger.info("Market data tables initialization complete.")


def _create_trading_days_table(db):
    db.create_table(
        """
        CREATE TABLE IF NOT EXISTS trading_days (
            ts TIMESTAMP,
            is_trading BOOLEAN NOT NULL
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL;
        """
    )


def _create_ohlc_table(db):
    freqs = ["1min", "5min", "15min", "30min", "60min", "d", "w", "m"]
    pm = {
        "1min": "HOUR",
        "5min": "HOUR",
        "15min": "DAY",
        "30min": "DAY",
        "60min": "DAY",
        "d": "MONTH",
        "w": "MONTH",
        "m": "YEAR",
    }
    for f in freqs:
        tn, cols = (
            get_ohlc_table_name(f),
            "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume LONG, amount LONG, code SYMBOL, market SYMBOL",
        )
        if not f.endswith("min"):
            cols = "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume LONG, amount LONG, turnover DOUBLE, code SYMBOL, market SYMBOL"
        db.create_table(
            f'CREATE TABLE IF NOT EXISTS "{tn}" ({cols}) TIMESTAMP(ts) PARTITION BY {pm[f]} WAL DEDUP UPSERT KEYS(ts, code, market)'
        )


def _create_stock_basic_table(db):
    create_sql = """CREATE TABLE IF NOT EXISTS "stock_basic_info" (ts TIMESTAMP, code SYMBOL, name STRING, fullname STRING, main_operation_biz STRING, affiliate_industry STRING, total_share DOUBLE, float_share DOUBLE, total_mv DOUBLE, float_mv DOUBLE, list_date TIMESTAMP) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)"""
    db.create_table(create_sql)


def _create_fund_basic_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "fund_basic_info" (ts TIMESTAMP, code SYMBOL, name STRING, fullname STRING, found_date TIMESTAMP, total_asset STRING, management STRING, fund_type SYMBOL, rating_agency STRING, rating STRING) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
    )


def _create_financial_indicators_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "financial_indicators" (ts TIMESTAMP, code SYMBOL, statDate STRING, pubDate STRING, roeAvg DOUBLE, npMargin DOUBLE, gpMargin DOUBLE, netProfit DOUBLE, epsTTM DOUBLE, MBRevenue DOUBLE, totalShare DOUBLE, liqaShare DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
    )


def _create_stock_industry_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "stock_industry" (ts TIMESTAMP, code SYMBOL, industry_name STRING, industry_classification STRING) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
    )


def _create_rolling_financial_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "rolling_financial" (ts TIMESTAMP, code SYMBOL, peTTM DOUBLE, psTTM DOUBLE, pcfNcfTTM DOUBLE, pbMRQ DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
    )


def _create_concept_constituents_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "concept_constituents" (ts TIMESTAMP, board_name STRING, board_code SYMBOL, stock_name STRING, stock_code SYMBOL) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, board_code, stock_code)'
    )


def _create_concept_boards_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "concept_boards" (ts TIMESTAMP, code SYMBOL, name STRING) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code)'
    )


def _create_concept_ohlc_table(db):
    db.create_table(
        'CREATE TABLE IF NOT EXISTS "concept_boards_ohlc" (ts TIMESTAMP, code SYMBOL, name STRING, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(ts, code)'
    )


if __name__ == "__main__":
    init_market_tables()
