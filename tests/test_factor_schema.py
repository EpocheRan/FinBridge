"""因子表 DDL 与写入列一致性。"""

from __future__ import annotations

import re

from scripts.schemas.factor_schema import (
    ALIAS_COLUMNS,
    AUDIT_STATE_COLUMNS,
    BUILD_STATE_COLUMNS,
    REGISTRY_COLUMNS,
    VALUE_ANNOUNCE_COLUMNS,
    VALUE_DAILY_COLUMNS,
    VALUE_LATEST_COLUMNS,
    create_factor_tables,
)

_COLUMN_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s+(?:STRING|SYMBOL|DOUBLE|LONG|INT|TIMESTAMP|BOOLEAN|FLOAT)",
    re.IGNORECASE,
)


def _ddl_columns(ddl: str) -> list[str]:
    body = ddl.split(") TIMESTAMP(")[0]
    body = body[body.index("(") + 1 :]
    return [match.group(1).lower() for match in _COLUMN_RE.finditer(body)]


def _find(statements, table_name):
    return next(sql for sql in statements if f'"{table_name}"' in sql)


def test_factor_ddl_matches_writer_columns():
    statements = create_factor_tables(None, dry_run=True)
    assert _ddl_columns(_find(statements, "factor_registry")) == REGISTRY_COLUMNS
    assert _ddl_columns(_find(statements, "factor_alias")) == ALIAS_COLUMNS
    assert _ddl_columns(_find(statements, "factor_value_daily")) == VALUE_DAILY_COLUMNS
    assert _ddl_columns(_find(statements, "factor_value_announce")) == VALUE_ANNOUNCE_COLUMNS
    assert _ddl_columns(_find(statements, "factor_value_latest")) == VALUE_LATEST_COLUMNS
    assert _ddl_columns(_find(statements, "factor_build_state")) == BUILD_STATE_COLUMNS
    assert _ddl_columns(_find(statements, "factor_audit_state")) == AUDIT_STATE_COLUMNS


def test_factor_dedup_keys_include_designated_timestamp():
    statements = create_factor_tables(None, dry_run=True)
    for sql in statements:
        if "DEDUP UPSERT KEYS" not in sql:
            continue
        designated = re.search(r"TIMESTAMP\((\w+)\)", sql).group(1)
        keys = re.search(r"DEDUP UPSERT KEYS\(([^)]+)\)", sql).group(1)
        assert designated in keys, f"dedup key 必须包含 designated timestamp: {designated}"


def test_value_tables_are_versioned():
    statements = create_factor_tables(None, dry_run=True)
    daily = next(sql for sql in statements if '"factor_value_daily"' in sql)
    announce = next(sql for sql in statements if '"factor_value_announce"' in sql)
    for sql in (daily, announce):
        assert "source_version" in sql
        assert "version" in sql
    assert "version, source_version" in daily
    # 事件表必须保留报告期与公告日
    assert "report_period" in announce
    assert "announce_date" in announce
