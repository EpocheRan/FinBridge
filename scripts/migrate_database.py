#!/usr/bin/env python
"""QuestDB 存量库迁移。

用途：把 2026-10 之前建立的行情表升级到「不复权价 + 复权因子」存储契约。

    # 1) 只体检，不改数据
    python scripts/migrate_database.py --check

    # 2) 加列 / 把 amount 从 LONG 重建为 DOUBLE
    python scripts/migrate_database.py --apply

    # 3) 迁移后会清空 OHLCV（旧行是前复权价，无法还原成 raw），再重跑同步
    python scripts/migrate_database.py --apply --truncate-ohlc
    python MCPserver.py   # 或让 Agent 调 scheduled_sync_a_shares 重新拉取

安全策略：

- 默认 ``--check``，不做任何写入。
- 检测到 ``adj_factor IS NULL`` 的历史行（= 旧的前复权数据）时，``--apply``
  只会警告并要求显式 ``--truncate-ohlc`` 或 ``--keep-legacy``；
  ``--keep-legacy`` 会保留这些行，读取端会把它们标成 ``adjust_fallback``。
- 重建表按「建临时表 → INSERT SELECT → DROP 旧表 → RENAME」执行，
  临时表名固定为 ``<table>__mig``，中断后可重复执行。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # Windows 控制台默认 GBK
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from data_interface.common.config import get_config  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from data_interface.market.constants import (  # noqa: E402
    OHLC_COLUMNS_DAILY,
    OHLC_COLUMNS_MINUTE,
    OHLC_TABLE_BY_FREQUENCY,
)
from scripts.schemas.market_schema import ohlc_table_ddl  # noqa: E402

logger = logging.getLogger("Scripts.Migrate")

FREQ_BY_TABLE = {table: freq for freq, table in OHLC_TABLE_BY_FREQUENCY.items()}


def _columns(db: QuestDBClient, table: str) -> dict[str, str] | None:
    try:
        rows = db.query(f'SHOW COLUMNS FROM "{table}"')
    except Exception:
        return None
    result: dict[str, str] = {}
    for row in rows:
        # SHOW COLUMNS: column, type, indexed, designatedTimestamp, upsertKey
        if len(row) >= 2:
            result[str(row[0])] = str(row[1]).upper()
    return result


def _row_count(db: QuestDBClient, table: str) -> int:
    try:
        row = db.query(f'SELECT count() FROM "{table}"', fetch_all=False)
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return -1


def _legacy_rows(db: QuestDBClient, table: str) -> int:
    try:
        row = db.query(
            f'SELECT count() FROM "{table}" WHERE adj_factor IS NULL', fetch_all=False
        )
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return -1


def _rebuild(db: QuestDBClient, table: str, freq: str, columns: dict[str, str], dry_run: bool) -> None:
    """重建表：修正 amount 类型并补 adj_factor。"""
    target_columns = (
        OHLC_COLUMNS_MINUTE if freq.endswith("min") else OHLC_COLUMNS_DAILY
    )
    temp = f"{table}__mig"
    select_parts = []
    for column in target_columns:
        if column == "amount":
            if column in columns:
                select_parts.append('CAST("amount" AS DOUBLE) AS "amount"')
            else:
                select_parts.append("CAST(0 AS DOUBLE) AS \"amount\"")
        elif column == "adj_factor":
            select_parts.append('"adj_factor"' if "adj_factor" in columns else "CAST(NULL AS DOUBLE) AS \"adj_factor\"")
        elif column in columns:
            select_parts.append(f'"{column}"')
        else:
            select_parts.append(f"CAST(NULL AS {_default_type(column)}) AS \"{column}\"")

    statements = [
        f'DROP TABLE IF EXISTS "{temp}"',
        ohlc_table_ddl(temp, freq),
        (
            f'INSERT INTO "{temp}" ({", ".join(chr(34) + c + chr(34) for c in target_columns)}) '
            f'SELECT {", ".join(select_parts)} FROM "{table}"'
        ),
        f'DROP TABLE "{table}"',
        f'RENAME TABLE "{temp}" TO "{table}"',
    ]
    for statement in statements:
        if dry_run:
            logger.info("[dry-run] %s", statement)
        else:
            logger.info("执行: %s", statement[:160])
            db.execute_ddl(statement)


def _default_type(column: str) -> str:
    if column in ("volume",):
        return "LONG"
    if column in ("code", "market"):
        return "SYMBOL"
    if column == "ts":
        return "TIMESTAMP"
    return "DOUBLE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FinBridge QuestDB 迁移脚本")
    parser.add_argument("--check", action="store_true", help="只体检（默认行为）")
    parser.add_argument("--apply", action="store_true", help="执行加列/重建表")
    parser.add_argument(
        "--truncate-ohlc",
        action="store_true",
        help="清空 OHLCV 表（旧行是前复权价，无法还原成 raw）",
    )
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="保留旧的 NULL 因子行（读取端会标记 adjust_fallback）",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    apply_changes = args.apply and not args.check

    config = get_config()
    db = QuestDBClient(config, connect=False)
    if not db.connect(host=args.host, port=args.port):
        logger.error("无法连接 QuestDB，请先启动数据库")
        return 3

    report: list[dict] = []
    legacy_total = 0
    try:
        for table, freq in FREQ_BY_TABLE.items():
            columns = _columns(db, table)
            if columns is None:
                logger.info("[%s] 表不存在，跳过（先跑 scripts/init_database.py）", table)
                report.append({"table": table, "state": "missing"})
                continue

            rows = _row_count(db, table)
            legacy = _legacy_rows(db, table) if "adj_factor" in columns else rows
            legacy_total += max(legacy, 0)
            amount_type = columns.get("amount", "?")
            needs_rebuild = amount_type not in ("DOUBLE", "?")
            needs_column = "adj_factor" not in columns

            logger.info(
                "[%s] rows=%s amount=%s adj_factor=%s legacy_null=%s",
                table,
                rows,
                amount_type,
                "ok" if not needs_column else "missing",
                legacy,
            )

            if apply_changes:
                if needs_rebuild:
                    logger.warning("[%s] amount 类型为 %s，重建为 DOUBLE", table, amount_type)
                    _rebuild(db, table, freq, columns, dry_run=False)
                elif needs_column:
                    db.execute_ddl(
                        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "adj_factor" DOUBLE'
                    )
                if args.truncate_ohlc:
                    db.execute_ddl(f'TRUNCATE TABLE "{table}"')
                    logger.warning("[%s] 已清空，等待重新同步 raw + adj_factor", table)

            report.append(
                {
                    "table": table,
                    "state": "ok",
                    "rows": rows,
                    "amount": amount_type,
                    "needs_rebuild": needs_rebuild,
                    "needs_adj_column": needs_column,
                    "legacy_rows": legacy,
                }
            )
    finally:
        db.close()

    if not apply_changes:
        print("\n=== 体检结果 ===")
        for item in report:
            if item["state"] == "missing":
                print(f"{item['table']:>10}: 表不存在")
            else:
                flags = []
                if item["needs_rebuild"]:
                    flags.append(f"amount={item['amount']} 需重建")
                if item["needs_adj_column"]:
                    flags.append("缺 adj_factor 列")
                if item["legacy_rows"]:
                    flags.append(f"{item['legacy_rows']} 行旧前复权数据(adj_factor IS NULL)")
                print(f"{item['table']:>10}: rows={item['rows']} " + ("; ".join(flags) or "已就绪"))

    if legacy_total > 0 and not args.truncate_ohlc and not args.keep_legacy:
        print(
            "\n检测到旧的前复权历史行（adj_factor 为空）。它们无法还原成不复权价：\n"
            "  - 要彻底修正，请加 --truncate-ohlc 清空后重新同步；\n"
            "  - 只想先迁移结构，请加 --keep-legacy（读取端会标记 adjust_fallback）。"
        )
        if apply_changes:
            return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
