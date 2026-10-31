"""资金与情绪表结构（两融 / 龙虎榜 / 大宗交易）。

三张表都由 ``acquisition`` 的 ``margin_detail`` / ``top_list`` /
``block_trade`` 数据集按交易日批量同步，WAL + DEDUP 幂等覆盖。

``block_trade`` 的 DEDUP 键包含 ``buyer``：同一只股票同一天可能有多笔
不同买方的大宗交易，只按 ``(ts, code)`` 会互相覆盖。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.SentimentSchema")

MARGIN_DETAIL_COLUMNS = [
    "ts",
    "code",
    "rzye",
    "rqye",
    "rzmre",
    "rqyl",
    "rzche",
    "rqchl",
]

TOP_LIST_COLUMNS = [
    "ts",
    "code",
    "name",
    "close",
    "pct_chg",
    "turnover_rate",
    "amount",
    "l_sell",
    "l_buy",
    "l_amount",
    "net_amount",
    "reason",
]

BLOCK_TRADE_COLUMNS = [
    "ts",
    "code",
    "price",
    "vol",
    "amount",
    "buyer",
    "seller",
]


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "margin_detail" (
            ts TIMESTAMP,
            code SYMBOL,
            rzye DOUBLE,
            rqye DOUBLE,
            rzmre DOUBLE,
            rqyl DOUBLE,
            rzche DOUBLE,
            rqchl DOUBLE
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code)
        """,
        """
        CREATE TABLE IF NOT EXISTS "top_list" (
            ts TIMESTAMP,
            code SYMBOL,
            name VARCHAR,
            close DOUBLE,
            pct_chg DOUBLE,
            turnover_rate DOUBLE,
            amount DOUBLE,
            l_sell DOUBLE,
            l_buy DOUBLE,
            l_amount DOUBLE,
            net_amount DOUBLE,
            reason VARCHAR
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code)
        """,
        """
        CREATE TABLE IF NOT EXISTS "block_trade" (
            ts TIMESTAMP,
            code SYMBOL,
            price DOUBLE,
            vol DOUBLE,
            amount DOUBLE,
            buyer VARCHAR,
            seller VARCHAR
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code, buyer)
        """,
    ]


def create_sentiment_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
