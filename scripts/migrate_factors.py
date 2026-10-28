#!/usr/bin/env python
"""M3：把 screener_daily 一次性迁移为 factor_daily_wide，并注册 legacy 因子。

策略（architecture_v2.md）：
- 宽表**重命名**（RENAME），不 melt 成 197M 行长表；
- 旧表名消失，screener 只读写 factor_daily_wide；
- 旧 56 列中可因子化的列注册到 factor_registry/factor_alias（version=daily-v1）；
- 不双写、不回读旧表。

用法：
    python scripts/migrate_factors.py            # dry-run
    python scripts/migrate_factors.py --apply    # 执行
    python scripts/migrate_factors.py --verify   # 校验
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from data_interface.common.config import get_config  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from factor_engine.legacy import legacy_specs  # noqa: E402
from factor_engine.store import FactorStore  # noqa: E402

OLD_TABLE = "screener_daily"
NEW_TABLE = "factor_daily_wide"


def table_exists(db: QuestDBClient, table: str) -> bool:
    try:
        rows = db.query(f'SELECT count() FROM "{table}"', fetch_all=False)
        return rows is not None
    except Exception:
        return False


def table_count(db: QuestDBClient, table: str) -> int | None:
    try:
        row = db.query(f'SELECT count() FROM "{table}"', fetch_all=False)
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移 screener_daily -> factor_daily_wide")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="执行迁移")
    group.add_argument("--verify", action="store_true", help="只校验")
    args = parser.parse_args()

    db = QuestDBClient(get_config())
    old_count = table_count(db, OLD_TABLE)
    new_count = table_count(db, NEW_TABLE)
    specs = legacy_specs()

    if args.verify or not args.apply:
        print(f"old={OLD_TABLE} rows={old_count}")
        print(f"new={NEW_TABLE} rows={new_count}")
        print(f"legacy_specs={len(specs)}")
        if old_count is not None and new_count is not None:
            print("WARNING: both tables exist; migration not applied or partially applied")
        return 0

    if old_count is None:
        print(f"{OLD_TABLE} 不存在，无法迁移")
        return 2
    if new_count is not None:
        print(f"{NEW_TABLE} 已存在，拒绝重复迁移")
        return 2

    print(f"RENAME {OLD_TABLE} -> {NEW_TABLE} ({old_count} rows)")
    db.execute_ddl(f'RENAME TABLE "{OLD_TABLE}" TO "{NEW_TABLE}"')

    store = FactorStore(db=db)
    store.upsert_registry(specs)
    store.upsert_aliases(
        [(alias, spec.factor_id, spec.window) for spec in specs for alias in spec.aliases]
    )

    print(
        f"migrated rows={table_count(db, NEW_TABLE)} "
        f"registry={len(specs)} aliases={sum(len(spec.aliases) for spec in specs)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
