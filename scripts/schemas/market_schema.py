"""行情相关表结构（QuestDB）。"""

from __future__ import annotations

import logging

from data_interface.market.constants import OHLC_TABLE_BY_FREQUENCY

logger = logging.getLogger("Scripts.MarketSchema")

OHLC_PARTITION = {
    "1min": "HOUR",
    "5min": "HOUR",
    "15min": "DAY",
    "30min": "DAY",
    "60min": "DAY",
    "d": "MONTH",
    "w": "MONTH",
    "m": "YEAR",
}


def _ohlc_sql(table: str, freq: str) -> str:
    partition = OHLC_PARTITION[freq]
    columns = (
        "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume LONG, amount LONG, code SYMBOL, market SYMBOL"
    )
    if not freq.endswith("min"):
        columns = (
            "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume LONG, amount LONG, turnover DOUBLE, code SYMBOL, market SYMBOL"
        )
    return (
        f'CREATE TABLE IF NOT EXISTS "{table}" ({columns}) '
        f"TIMESTAMP(ts) PARTITION BY {partition} WAL "
        "DEDUP UPSERT KEYS(ts, code, market)"
    )


def create_market_tables(db, *, dry_run: bool = False) -> list[str]:
    statements: list[str] = []

    for freq, table in OHLC_TABLE_BY_FREQUENCY.items():
        statements.append(_ohlc_sql(table, freq))

    statements.extend(
        [
            """
            CREATE TABLE IF NOT EXISTS "stock_basic_info" (
                ts TIMESTAMP,
                code SYMBOL,
                name STRING,
                fullname STRING,
                exchange SYMBOL,
                board SYMBOL,
                market SYMBOL,
                security_type SYMBOL,
                currency SYMBOL,
                is_st BOOLEAN,
                list_status SYMBOL,
                list_date TIMESTAMP,
                delist_date TIMESTAMP,
                industry STRING,
                sw_l1 STRING,
                sw_l2 STRING,
                sw_l3 STRING,
                area STRING,
                main_business STRING,
                total_share DOUBLE,
                float_share DOUBLE,
                total_mv DOUBLE,
                float_mv DOUBLE,
                source SYMBOL,
                updated_at TIMESTAMP
            ) TIMESTAMP(ts) PARTITION BY MONTH WAL
            DEDUP UPSERT KEYS(code, ts)
            """,
            """
            CREATE TABLE IF NOT EXISTS "fund_basic_info" (
                ts TIMESTAMP,
                code SYMBOL,
                name STRING,
                fullname STRING,
                found_date TIMESTAMP,
                total_asset STRING,
                management STRING,
                fund_type SYMBOL,
                rating_agency STRING,
                rating STRING
            ) TIMESTAMP(ts) PARTITION BY MONTH WAL
            DEDUP UPSERT KEYS(code, ts)
            """,
            """
            CREATE TABLE IF NOT EXISTS "concept_boards" (
                ts TIMESTAMP,
                code SYMBOL,
                name STRING
            ) TIMESTAMP(ts) PARTITION BY YEAR WAL
            DEDUP UPSERT KEYS(ts, code)
            """,
            """
            CREATE TABLE IF NOT EXISTS "concept_boards_ohlc" (
                ts TIMESTAMP,
                code SYMBOL,
                name STRING,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume DOUBLE,
                amount DOUBLE
            ) TIMESTAMP(ts) PARTITION BY MONTH WAL
            DEDUP UPSERT KEYS(ts, code)
            """,
            """
            CREATE TABLE IF NOT EXISTS "concept_constituents" (
                ts TIMESTAMP,
                board_name STRING,
                board_code SYMBOL,
                stock_name STRING,
                stock_code SYMBOL
            ) TIMESTAMP(ts) PARTITION BY YEAR WAL
            DEDUP UPSERT KEYS(ts, board_code, stock_code)
            """,
            """
            CREATE TABLE IF NOT EXISTS "trading_days" (
                ts TIMESTAMP,
                is_trading BOOLEAN NOT NULL
            ) TIMESTAMP(ts) PARTITION BY YEAR WAL
            DEDUP UPSERT KEYS(ts)
            """,
        ]
    )

    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
