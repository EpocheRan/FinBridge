"""表结构一致性测试：写入列集合必须与 DDL 完全一致。

历史上出过的问题：
- ``update_stock_basic`` 写 ``main_operation_biz`` / ``affiliate_industry``，
  DDL 里只有 ``main_business`` / ``industry`` → 每次 INSERT 都失败；
- OHLCV 表新增 ``adj_factor`` 后，写入端与建表 DDL 必须同步。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from data_interface.financial.fields import PROFILE_COLUMNS
from data_interface.market.constants import (
    OHLC_COLUMNS_DAILY,
    OHLC_COLUMNS_MINUTE,
    OHLC_TABLE_BY_FREQUENCY,
)
from scripts.schemas.market_schema import create_market_tables, ohlc_table_ddl

_COLUMN_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s+(?:STRING|SYMBOL|DOUBLE|LONG|INT|TIMESTAMP|BOOLEAN|FLOAT)",
    re.IGNORECASE,
)


def _ddl_columns(ddl: str) -> list[str]:
    body = ddl.split(") TIMESTAMP(")[0]
    body = body[body.index("(") + 1 :]
    return [match.group(1).lower() for match in _COLUMN_RE.finditer(body)]


@pytest.mark.parametrize("freq,table", list(OHLC_TABLE_BY_FREQUENCY.items()))
def test_ohlc_ddl_matches_writer_columns(freq: str, table: str):
    expected = OHLC_COLUMNS_MINUTE if freq.endswith("min") else OHLC_COLUMNS_DAILY
    assert _ddl_columns(ohlc_table_ddl(table, freq)) == expected


def test_ohlc_ddl_declares_raw_price_storage():
    columns = _ddl_columns(ohlc_table_ddl("ohlcv_1d", "d"))
    assert "adj_factor" in columns
    # 存储契约：不复权价 + 因子；不需要额外的 adjust 标记列
    assert "adjust" not in columns


def test_stock_basic_ddl_matches_profile_columns():
    statements = create_market_tables(None, dry_run=True)
    stock_basic = next(sql for sql in statements if '"stock_basic_info"' in sql)
    assert _ddl_columns(stock_basic) == PROFILE_COLUMNS


def test_writers_do_not_use_columns_missing_from_ddl():
    statements = create_market_tables(None, dry_run=True)
    joined = "\n".join(statements)
    assert "main_operation_biz" not in joined
    assert "affiliate_industry" not in joined

    root = Path(__file__).resolve().parents[1]
    writers = [
        root / "data_interface" / "market" / "fetcher.py",
        root / "data_interface" / "financial" / "repository.py",
        root / "data_interface" / "financial" / "fields.py",
    ]
    for path in writers:
        text = path.read_text(encoding="utf-8")
        assert "main_operation_biz" not in text, f"{path} 仍在使用 DDL 中不存在的列名"
        assert "affiliate_industry" not in text, f"{path} 仍在使用 DDL 中不存在的列名"


def test_dedup_keys_include_designated_timestamp():
    statements = create_market_tables(None, dry_run=True)
    for sql in statements:
        if "DEDUP UPSERT KEYS" not in sql:
            continue
        assert "TIMESTAMP(" in sql
        designated = re.search(r"TIMESTAMP\((\w+)\)", sql).group(1)
        keys = re.search(r"DEDUP UPSERT KEYS\(([^)]+)\)", sql).group(1)
        assert designated in keys, f"dedup key 必须包含 designated timestamp: {designated}"
