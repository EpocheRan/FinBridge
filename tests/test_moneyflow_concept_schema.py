"""分档 / 概念表结构一致性测试：DDL 列必须与写入列完全一致。"""

from __future__ import annotations

import re

from scripts.schemas.concept_schema import (
    CONCEPT_ALIAS_COLUMNS,
    CONCEPT_BOARD_COLUMNS,
    CONCEPT_CLASS_COLUMNS,
    CONCEPT_DAILY_COLUMNS,
    CONCEPT_MEMBER_COLUMNS,
    create_concept_tables,
)
from scripts.schemas.moneyflow_schema import (
    MONEYFLOW_COLUMNS,
    MONEYFLOW_SOURCE_COLUMNS,
    create_moneyflow_tables,
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


def test_moneyflow_ddl_matches_writer_columns():
    statements = create_moneyflow_tables(None, dry_run=True)
    assert _ddl_columns(_find(statements, "moneyflow_daily")) == MONEYFLOW_COLUMNS
    assert _ddl_columns(_find(statements, "moneyflow_source")) == MONEYFLOW_SOURCE_COLUMNS


def test_concept_ddl_matches_writer_columns():
    statements = create_concept_tables(None, dry_run=True)
    assert _ddl_columns(_find(statements, "concept_board_dim")) == CONCEPT_BOARD_COLUMNS
    assert _ddl_columns(_find(statements, "concept_board_daily")) == CONCEPT_DAILY_COLUMNS
    assert _ddl_columns(_find(statements, "concept_member_snapshot")) == CONCEPT_MEMBER_COLUMNS
    assert _ddl_columns(_find(statements, "concept_classification")) == CONCEPT_CLASS_COLUMNS
    assert _ddl_columns(_find(statements, "concept_board_alias")) == CONCEPT_ALIAS_COLUMNS


def test_all_new_tables_dedup_include_designated_timestamp():
    statements = create_moneyflow_tables(None, dry_run=True) + create_concept_tables(
        None, dry_run=True
    )
    for sql in statements:
        if "DEDUP UPSERT KEYS" not in sql:
            continue
        designated = re.search(r"TIMESTAMP\((\w+)\)", sql).group(1)
        keys = re.search(r"DEDUP UPSERT KEYS\(([^)]+)\)", sql).group(1)
        assert designated in keys, f"dedup key 必须包含 designated timestamp: {designated}"
