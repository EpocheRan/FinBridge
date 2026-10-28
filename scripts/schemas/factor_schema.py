"""因子库相关表结构（M0）。

表归属：
- 本文件的表全部由 ``factor_engine`` 写入；
- raw 表（行情/财务/分档/概念）仍由 ``data_interface`` 写，见 market/financial/moneyflow/concept schema。

版本化约定：
- ``factor_value_daily`` / ``factor_value_announce`` 的 DEDUP 键包含
  ``version`` 与 ``source_version``，版本升级写新行，旧行保留。
- ``factor_value_latest`` 是读模型（最新日缓存），查询用
  ``LATEST ON ts PARTITION BY code, factor_id``，由 factor_engine 定期裁剪。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.FactorSchema")

REGISTRY_COLUMNS = [
    "updated_at",
    "factor_id",
    "name",
    "category",
    "subcategory",
    "source_dataset",
    "source_vendor",
    "source_fields",
    "freq",
    "window",
    "lag",
    "value_kind",
    "update_mode",
    "unit",
    "direction",
    "price_basis",
    "version",
    "source_version_policy",
    "dependencies",
    "warmup",
    "formula",
    "enabled",
    "materialization",
]

ALIAS_COLUMNS = [
    "updated_at",
    "alias",
    "factor_id",
    "window",
    "params",
    "notes",
]

VALUE_DAILY_COLUMNS = [
    "ts",
    "code",
    "factor_id",
    "value",
    "quality",
    "source",
    "source_version",
    "version",
    "computed_at",
]

VALUE_ANNOUNCE_COLUMNS = [
    "ts",
    "code",
    "factor_id",
    "report_period",
    "announce_date",
    "lag_days",
    "value",
    "quality",
    "source",
    "source_version",
    "version",
    "computed_at",
]

VALUE_LATEST_COLUMNS = [
    "ts",
    "code",
    "factor_id",
    "value",
    "quality",
    "source",
    "source_version",
    "version",
    "computed_at",
]

BUILD_STATE_COLUMNS = [
    "updated_at",
    "job_id",
    "dataset",
    "factor_ids",
    "window_start",
    "window_end",
    "cursor",
    "status",
    "attempts",
    "retry_at",
    "rows_written",
    "last_error",
    "payload",
]

AUDIT_STATE_COLUMNS = [
    "updated_at",
    "audit_id",
    "dataset",
    "factor_ids",
    "window_start",
    "window_end",
    "status",
    "checked",
    "failed",
    "max_abs_diff",
    "max_rel_diff",
    "details",
    "payload",
]


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "factor_registry" (
            updated_at TIMESTAMP,
            factor_id SYMBOL,
            name STRING,
            category SYMBOL,
            subcategory SYMBOL,
            source_dataset SYMBOL,
            source_vendor SYMBOL,
            source_fields STRING,
            freq SYMBOL,
            window INT,
            lag INT,
            value_kind SYMBOL,
            update_mode SYMBOL,
            unit SYMBOL,
            direction SYMBOL,
            price_basis SYMBOL,
            version SYMBOL,
            source_version_policy STRING,
            dependencies STRING,
            warmup INT,
            formula STRING,
            enabled BOOLEAN,
            materialization SYMBOL
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, factor_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_alias" (
            updated_at TIMESTAMP,
            alias SYMBOL,
            factor_id SYMBOL,
            window INT,
            params STRING,
            notes STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, alias)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_value_daily" (
            ts TIMESTAMP,
            code SYMBOL,
            factor_id SYMBOL,
            value DOUBLE,
            quality SYMBOL,
            source SYMBOL,
            source_version SYMBOL,
            version SYMBOL,
            computed_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, code, factor_id, version, source_version)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_value_announce" (
            ts TIMESTAMP,
            code SYMBOL,
            factor_id SYMBOL,
            report_period TIMESTAMP,
            announce_date TIMESTAMP,
            lag_days INT,
            value DOUBLE,
            quality SYMBOL,
            source SYMBOL,
            source_version SYMBOL,
            version SYMBOL,
            computed_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code, factor_id, report_period, version, source_version)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_value_latest" (
            ts TIMESTAMP,
            code SYMBOL,
            factor_id SYMBOL,
            value DOUBLE,
            quality SYMBOL,
            source SYMBOL,
            source_version SYMBOL,
            version SYMBOL,
            computed_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code, factor_id, version, source_version)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_build_state" (
            updated_at TIMESTAMP,
            job_id SYMBOL,
            dataset SYMBOL,
            factor_ids STRING,
            window_start TIMESTAMP,
            window_end TIMESTAMP,
            cursor TIMESTAMP,
            status SYMBOL,
            attempts INT,
            retry_at TIMESTAMP,
            rows_written LONG,
            last_error STRING,
            payload STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, job_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "factor_audit_state" (
            updated_at TIMESTAMP,
            audit_id SYMBOL,
            dataset SYMBOL,
            factor_ids STRING,
            window_start TIMESTAMP,
            window_end TIMESTAMP,
            status SYMBOL,
            checked LONG,
            failed LONG,
            max_abs_diff DOUBLE,
            max_rel_diff DOUBLE,
            details STRING,
            payload STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, audit_id)
        """,
    ]


def create_factor_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
