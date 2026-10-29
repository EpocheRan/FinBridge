"""披露日历与公告列表表结构。

设计要点
--------
- ``fin_disclosure_calendar`` 是**预约披露日**的时序快照：同一 ``(code, report_date)``
  会在不同快照日被反复写入（预约日可能改期），查询时用 ``LATEST ON ts`` 取最新。
- 一次预约可能被改 1~3 次，故保留 ``first/second/third_change_date``，
  改期次数本身就是「财报不确定性」的信号。
- ``announcement`` 以 ``art_code`` 为业务主键，同一公告当天可能被多条渠道重复收录，
  用 ``DEDUP UPSERT KEYS(ts, code, art_code)`` 去重。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.DisclosureSchema")

DISCLOSURE_COLUMNS = [
    "ts",
    "code",
    "report_date",
    "report_type",
    "appoint_date",
    "first_appoint_date",
    "first_change_date",
    "second_change_date",
    "third_change_date",
    "actual_date",
    "residual_days",
    "is_published",
    "market",
    "source",
    "fetched_at",
]

ANNOUNCEMENT_COLUMNS = [
    "ts",
    "code",
    "art_code",
    "title",
    "ann_type",
    "column_code",
    "column_name",
    "display_time",
    "source",
    "fetched_at",
]


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "fin_disclosure_calendar" (
            ts TIMESTAMP,
            code SYMBOL,
            report_date TIMESTAMP,
            report_type STRING,
            appoint_date TIMESTAMP,
            first_appoint_date TIMESTAMP,
            first_change_date TIMESTAMP,
            second_change_date TIMESTAMP,
            third_change_date TIMESTAMP,
            actual_date TIMESTAMP,
            residual_days INT,
            is_published BOOLEAN,
            market SYMBOL,
            source SYMBOL,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code, report_date, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "announcement" (
            ts TIMESTAMP,
            code SYMBOL,
            art_code SYMBOL,
            title STRING,
            ann_type STRING,
            column_code STRING,
            column_name STRING,
            display_time TIMESTAMP,
            source SYMBOL,
            fetched_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code, art_code)
        """,
    ]


def create_disclosure_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
