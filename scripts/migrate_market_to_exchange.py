"""把 moneyflow_daily / market_suspension / factor_daily_wide / stock_basic_info
的 market 列从 MLCN / A 改成交易所代码（SSE / SZSE / BSE）。

做法：按 SHOW CREATE TABLE 的 DDL 建同名 _new 表（保留 timestamp/分区/DEDUP 键），
INSERT ... SELECT 重写，校验行数一致后 RENAME 交换，旧表保留为 _old。
"""
from __future__ import annotations

import json
import sys
import time

import requests

BASE = "http://127.0.0.1:9000"

EXCH = """case
    when market='IDX' then (case when left(code,3)='399' then 'SZSE' else 'SSE' end)
    when left(code,2) in ('43','83','87','92','82','88') then 'BSE'
    when left(code,2) in ('60','68','90','50','51','52','53','55','56','58') then 'SSE'
    when left(code,2) in ('00','30','20','15','16','18','12','39') then 'SZSE'
    when left(code,1)='9' then 'SSE'
    else 'SZSE' end"""

TABLES = [
    ("moneyflow_daily", EXCH),
    ("market_suspension", EXCH),
    ("factor_daily_wide", EXCH),
    ("stock_basic_info", f"coalesce(exchange, {EXCH})"),
]


def q(sql: str, timeout: int = 1800) -> dict:
    r = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()


def count(table: str) -> int:
    return q(f"select count() c from {table}")["dataset"][0][0]


def columns(table: str) -> list[str]:
    return [c["name"] for c in q(f"select * from {table} limit 1")["columns"]]


def ddl_of(table: str) -> str:
    return q(f"SHOW CREATE TABLE {table}")["dataset"][0][0]


def main() -> int:
    only = sys.argv[1:] or [t for t, _ in TABLES]
    for table, expr in TABLES:
        if table not in only:
            continue
        t0 = time.time()
        print(f"\n=== {table}")
        n_src = count(table)
        print(f"  源行数 {n_src:,}")

        if q(f"select count() c from tables() where table_name='{table}_new'")["dataset"][0][0]:
            q(f"DROP TABLE {table}_new")
        ddl = ddl_of(table).replace(f"'{table}'", f"'{table}_new'", 1)
        if "WAL" not in ddl.upper():
            ddl = ddl.replace("DEDUP UPSERT", "WAL DEDUP UPSERT")
        q(ddl)
        print("  已建新表")

        cols = columns(table)
        select_list = ", ".join(f"{expr} as market" if c == "market" else f'"{c}"' for c in cols)
        q(f'INSERT INTO {table}_new SELECT {select_list} FROM {table}')
        # 等待 WAL 落盘
        for _ in range(30):
            n_new = count(f"{table}_new")
            if n_new >= n_src:
                break
            time.sleep(2)
        n_new = count(f"{table}_new")
        print(f"  新表行数 {n_new:,}")
        if n_new != n_src:
            print("  !! 行数不一致，终止")
            return 1

        dist = q(f"select market, count() c from {table}_new group by market")
        print("  分布:", [(r[0], r[1]) for r in dist["dataset"]])

        q(f"RENAME TABLE {table} TO {table}_old")
        q(f"RENAME TABLE {table}_new TO {table}")
        print(f"  已交换（旧表 {table}_old），用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
