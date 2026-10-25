"""定时任务状态表。"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.TaskSchema")


def create_task_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS "scheduled_task_state" (
            updated_at TIMESTAMP,
            task_name SYMBOL,
            dataset SYMBOL,
            cursor STRING,
            status SYMBOL,
            total INT,
            processed INT,
            message STRING,
            payload STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, task_name, dataset)
        """
    ]
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
