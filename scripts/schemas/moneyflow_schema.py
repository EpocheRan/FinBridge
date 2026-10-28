"""分档（资金流）相关表结构。

口径约定：

- 金额一律「元」、量一律「股」；源数据是万元/手的在 adapter 内换算。
- 同一 ``(ts, code, market, source)`` 幂等覆盖，允许多个数据源并存。
- ``source`` 命名：``{vendor}.{dataset}[.{variant}]``，如 ``tushare.moneyflow``。
- ``moneyflow_source`` 是源注册表：记录口径、覆盖区间、优先级。

注意：``net_mf_amount`` 与四档净额是不同口径（Tushare 实测两者不相等），
派生指标必须显式声明用哪一个。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.MoneyflowSchema")

#: ``moneyflow_daily`` 的写入列（与 DDL 顺序一致，测试会逐列比对）
MONEYFLOW_COLUMNS = [
    "ts",
    "code",
    "market",
    "buy_sm_vol",
    "buy_sm_amount",
    "sell_sm_vol",
    "sell_sm_amount",
    "buy_md_vol",
    "buy_md_amount",
    "sell_md_vol",
    "sell_md_amount",
    "buy_lg_vol",
    "buy_lg_amount",
    "sell_lg_vol",
    "sell_lg_amount",
    "buy_elg_vol",
    "buy_elg_amount",
    "sell_elg_vol",
    "sell_elg_amount",
    "net_mf_vol",
    "net_mf_amount",
    "source",
    "source_version",
    "quality",
    "quality_note",
    "fetched_at",
]

#: ``moneyflow_source`` 的写入列
MONEYFLOW_SOURCE_COLUMNS = [
    "updated_at",
    "source",
    "vendor",
    "dataset",
    "unit",
    "definition",
    "coverage_start",
    "coverage_end",
    "priority",
    "enabled",
    "notes",
]


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "moneyflow_daily" (
            ts TIMESTAMP,
            code SYMBOL,
            market SYMBOL,

            buy_sm_vol LONG,
            buy_sm_amount DOUBLE,
            sell_sm_vol LONG,
            sell_sm_amount DOUBLE,
            buy_md_vol LONG,
            buy_md_amount DOUBLE,
            sell_md_vol LONG,
            sell_md_amount DOUBLE,
            buy_lg_vol LONG,
            buy_lg_amount DOUBLE,
            sell_lg_vol LONG,
            sell_lg_amount DOUBLE,
            buy_elg_vol LONG,
            buy_elg_amount DOUBLE,
            sell_elg_vol LONG,
            sell_elg_amount DOUBLE,

            net_mf_vol LONG,
            net_mf_amount DOUBLE,

            source SYMBOL,
            source_version SYMBOL,
            quality SYMBOL,
            quality_note STRING,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, code, market, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "moneyflow_source" (
            updated_at TIMESTAMP,
            source SYMBOL,
            vendor SYMBOL,
            dataset SYMBOL,
            unit STRING,
            definition STRING,
            coverage_start TIMESTAMP,
            coverage_end TIMESTAMP,
            priority INT,
            enabled BOOLEAN,
            notes STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, source)
        """,
    ]


def create_moneyflow_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
