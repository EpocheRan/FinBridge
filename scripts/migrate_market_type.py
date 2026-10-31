"""把 ohlcv_1d 迁移到「market=交易所 + type=品种」的新 schema。

步骤：
 1. 建 ohlcv_1d_new（多一个 type 列，DEDUP 键扩到 ts/code/market/type）
 2. 按年份分块 INSERT ... SELECT（QuestDB 单条 INSERT..SELECT 有 1100 万行上限）
 3. 校验行数与分布
 4. 交换表名（旧表保留为 ohlcv_1d_old）
"""
from __future__ import annotations

import json
import sys
import time

import requests

BASE = "http://127.0.0.1:9000"

EXCH_CASE = """case
    when market='IDX' then (case when left(code,3)='399' then 'SZSE' else 'SSE' end)
    when left(code,2) in ('43','83','87','92','82','88') then 'BSE'
    when left(code,2) in ('60','68','90','50','51','52','53','55','56','58') then 'SSE'
    when left(code,2) in ('00','30','20','15','16','18','12','39') then 'SZSE'
    when left(code,1)='9' then 'SSE'
    else 'SZSE' end"""

TYPE_CASE = """case
    when market='IDX' then 'idx'
    when left(code,3) in ('501','502','506') or left(code,2)='16' then 'lof'
    when left(code,2) in ('51','52','53','55','56','58') or left(code,3)='159' then 'etf'
    when left(code,3) in ('508','180','184','500','150') or left(code,2)='50' then 'fund'
    when left(code,2) in ('15','18') then 'etf'
    else 'stock' end"""

CHUNKS = [("1990-01-01", "2000-01-01"), ("2000-01-01", "2010-01-01"), ("2010-01-01", "2015-01-01"),
          ("2015-01-01", "2016-01-01"), ("2016-01-01", "2017-01-01"), ("2017-01-01", "2018-01-01"),
          ("2018-01-01", "2019-01-01"), ("2019-01-01", "2020-01-01"), ("2020-01-01", "2021-01-01"),
          ("2021-01-01", "2022-01-01"), ("2022-01-01", "2023-01-01"), ("2023-01-01", "2024-01-01"),
          ("2024-01-01", "2025-01-01"), ("2025-01-01", "2026-01-01"), ("2026-01-01", "2027-01-01")]


def q(sql: str, timeout: int = 3600) -> str:
    r = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    r.raise_for_status()
    return r.text


def count(table: str = "ohlcv_1d") -> int:
    return json.loads(q(f"select count() c from {table}"))["dataset"][0][0]


def main() -> int:
    t0 = time.time()
    print("1) create ohlcv_1d_new ...", flush=True)
    q("""
    CREATE TABLE IF NOT EXISTS ohlcv_1d_new (
        ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        volume LONG, amount DOUBLE, turnover DOUBLE, adj_factor DOUBLE,
        code SYMBOL, market SYMBOL, type SYMBOL
    ) timestamp(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(ts, code, market, type)
    """)
    q('TRUNCATE TABLE ohlcv_1d_new')
    print("   truncated", flush=True)

    total_src = count("ohlcv_1d")
    print(f"2) rewrite {total_src:,} rows in {len(CHUNKS)} chunks ...", flush=True)
    for lo, hi in CHUNKS:
        q(f"""
        INSERT INTO ohlcv_1d_new
        SELECT ts, open, high, low, close, volume, amount, turnover, adj_factor, code,
               {EXCH_CASE} AS market,
               {TYPE_CASE} AS type
        FROM ohlcv_1d WHERE ts >= '{lo}' AND ts < '{hi}'
        """)
        print(f"   {lo[:4]}-{hi[:4]}: cum={count('ohlcv_1d_new'):,} ({time.time()-t0:.0f}s)", flush=True)

    n_src, n_dst = total_src, count("ohlcv_1d_new")
    print(f"3) verify: src={n_src:,} dst={n_dst:,}", flush=True)
    if n_src != n_dst:
        print(f"   FAIL: row count mismatch, abort (old table untouched)")
        return 1
    print("   OK", flush=True)
    print("   dist:", q("select market, type, count() c from ohlcv_1d_new group by market, type order by market")[:1200])

    print("4) swap table names", flush=True)
    q('ALTER TABLE ohlcv_1d RENAME TO ohlcv_1d_old')
    q('ALTER TABLE ohlcv_1d_new RENAME TO ohlcv_1d')
    print("   done:", q("select market, type, count() c from ohlcv_1d group by market, type order by market")[:1200])
    print(f"total {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
