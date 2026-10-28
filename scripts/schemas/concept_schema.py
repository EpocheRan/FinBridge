"""概念板块相关表结构。

设计要点：

- 板块维度按日快照（``concept_board_dim``），保留名称/状态变更。
- 板块日线自带 ``source``，同一天允许不同源并存。
- 成分是「整组快照」：每次同步把同一 ``ts`` 的全量成分写入，
  PIT 查询取 ``<= as_of`` 的最近快照日整组返回，被剔除的股票不会残留。
- 跨源代码（同花顺 / 东财 / Tushare）不通用，统一走 ``concept_board_alias``。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.ConceptSchema")

CONCEPT_BOARD_COLUMNS = [
    "ts",
    "board_code",
    "name",
    "board_type",
    "source",
    "source_code",
    "status",
    "updated_at",
]

CONCEPT_DAILY_COLUMNS = [
    "ts",
    "board_code",
    "board_name",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "quality",
    "source",
    "fetched_at",
]

CONCEPT_MEMBER_COLUMNS = [
    "ts",
    "board_code",
    "stock_code",
    "stock_name",
    "weight",
    "in_date",
    "quality",
    "source",
    "fetched_at",
]

CONCEPT_CLASS_COLUMNS = [
    "ts",
    "concept_code",
    "name",
    "class1_code",
    "class1_name",
    "class2_code",
    "class2_name",
    "class3_code",
    "class3_name",
    "item_size",
    "source",
    "fetched_at",
]

CONCEPT_ALIAS_COLUMNS = [
    "updated_at",
    "board_code",
    "source",
    "source_code",
    "source_name",
    "confidence",
    "verified",
    "notes",
]


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "concept_board_dim" (
            ts TIMESTAMP,
            board_code SYMBOL,
            name STRING,
            board_type SYMBOL,
            source SYMBOL,
            source_code SYMBOL,
            status SYMBOL,
            updated_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, board_code, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "concept_board_daily" (
            ts TIMESTAMP,
            board_code SYMBOL,
            board_name STRING,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume LONG,
            amount DOUBLE,
            pct_chg DOUBLE,
            quality SYMBOL,
            source SYMBOL,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, board_code, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "concept_member_snapshot" (
            ts TIMESTAMP,
            board_code SYMBOL,
            stock_code SYMBOL,
            stock_name STRING,
            weight DOUBLE,
            in_date TIMESTAMP,
            quality SYMBOL,
            source SYMBOL,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, board_code, stock_code, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "concept_classification" (
            ts TIMESTAMP,
            concept_code SYMBOL,
            name STRING,
            class1_code SYMBOL,
            class1_name STRING,
            class2_code SYMBOL,
            class2_name STRING,
            class3_code SYMBOL,
            class3_name STRING,
            item_size INT,
            source SYMBOL,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, concept_code, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "concept_board_alias" (
            updated_at TIMESTAMP,
            board_code SYMBOL,
            source SYMBOL,
            source_code SYMBOL,
            source_name STRING,
            confidence DOUBLE,
            verified BOOLEAN,
            notes STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, board_code, source, source_code)
        """,
    ]


def create_concept_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
