"""持仓相关表结构（QuestDB）。

持仓表保留“原地 UPDATE”语义（应用层通过 REST UPDATE 修改最新记录），
因此不使用 WAL/DEDUP，避免与 QuestDB 的 UPDATE 语义产生冲突。
财务/行情表则使用 WAL + DEDUP，见对应 schema。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.PositionSchema")


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "portfolios" (
            id TIMESTAMP,
            portfolio_id SYMBOL,
            name STRING,
            total_assets DOUBLE,
            cash DOUBLE,
            market_value DOUBLE,
            unrealized_profit DOUBLE,
            realized_profit DOUBLE,
            deleted_at TIMESTAMP
        ) TIMESTAMP(id) PARTITION BY YEAR
        """,
        """
        CREATE TABLE IF NOT EXISTS "positions" (
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
        ) TIMESTAMP(id) PARTITION BY YEAR
        """,
        """
        CREATE TABLE IF NOT EXISTS "trade_records" (
            trade_id SYMBOL,
            pos_id SYMBOL,
            type SYMBOL,
            quantity DOUBLE,
            price DOUBLE,
            trade_date TIMESTAMP,
            updated_at TIMESTAMP,
            deleted_at TIMESTAMP
        ) TIMESTAMP(trade_date) PARTITION BY YEAR
        """,
    ]


def create_position_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)

    # 兼容旧库：补列失败不阻塞初始化
    for column, column_type in (
        ("stop_price", "DOUBLE"),
        ("first_entry_date", "TIMESTAMP"),
    ):
        if dry_run:
            continue
        try:
            db.execute_http(
                f'ALTER TABLE "positions" ADD COLUMN IF NOT EXISTS "{column}" {column_type}'
            )
        except Exception:
            pass
    return statements
