"""行情相关表结构（QuestDB）。

存储口径（2026-10 起）：

- OHLCV 表只存**不复权（raw）**价，复权由读取端按 ``adj_factor`` 计算
  （见 ``data_interface/market/adjust.py``）。``adj_factor`` 是累计后复权因子。
- ``DEDUP UPSERT KEYS(ts, code, market, type)``：同一根 bar 幂等覆盖。
- 金额 ``amount`` 为 DOUBLE（元）；``volume`` 为 LONG（股）。
"""

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

#: 需要在存量库上补齐的列（(列名, 类型)），迁移脚本与 init 共用
OHLC_EXTRA_COLUMNS = (("adj_factor", "DOUBLE"), ("type", "SYMBOL"))


def _ohlc_sql(table: str, freq: str) -> str:
    partition = OHLC_PARTITION[freq]
    columns = (
        "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume LONG, amount DOUBLE, adj_factor DOUBLE, code SYMBOL, market SYMBOL, type SYMBOL"
    )
    if not freq.endswith("min"):
        columns = (
            "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume LONG, amount DOUBLE, turnover DOUBLE, adj_factor DOUBLE, "
            "code SYMBOL, market SYMBOL, type SYMBOL"
        )
    return (
        f'CREATE TABLE IF NOT EXISTS "{table}" ({columns}) '
        f"TIMESTAMP(ts) PARTITION BY {partition} WAL "
        "DEDUP UPSERT KEYS(ts, code, market, type)"
    )


def ohlc_table_ddl(table: str, freq: str) -> str:
    """生成单张 OHLCV 表的 DDL（迁移脚本重建表时复用）。"""
    return _ohlc_sql(table, freq)


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


def migrate_market_tables(db, *, dry_run: bool = False) -> list[str]:
    """存量库补齐：给每张 OHLCV 表加 ``adj_factor``。"""
    statements = [
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "adj_factor" DOUBLE'
        for table in OHLC_TABLE_BY_FREQUENCY.values()
    ]
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql)
        else:
            db.execute_ddl(sql)
    return statements
