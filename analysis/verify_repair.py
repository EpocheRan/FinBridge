"""修复后的完整性验收。"""
from __future__ import annotations

import json

import requests

BASE = "http://127.0.0.1:9000"


def q(sql: str) -> dict:
    r = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=300)
    r.raise_for_status()
    return r.json()


def scalar(sql: str):
    return q(sql)["dataset"][0][0]


def rows(sql: str):
    return q(sql)["dataset"]


checks = []


def check(name: str, ok: bool, detail: str) -> None:
    checks.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


print("=== 1. 表结构与口径 ===")
cols = [c["name"] for c in q("select * from ohlcv_1d limit 1")["columns"]]
print("columns:", cols)
check("type 列存在", "type" in cols, f"columns={len(cols)}")

n_null = scalar("select count() c from ohlcv_1d where type is null")
check("type 无空值", n_null == 0, f"null={n_null}")

bad_market = scalar("select count() c from ohlcv_1d where market not in ('SSE','SZSE','BSE')")
check("market 只有三所", bad_market == 0, f"非法={bad_market}")

bad_type = scalar("select count() c from ohlcv_1d where type not in ('stock','etf','lof','fund','idx')")
check("type 取值合法", bad_type == 0, f"非法={bad_type}")

dist = rows("select market, type, count() c from ohlcv_1d group by market, type order by market, type")
print("   分布:")
for m, t, c in dist:
    print(f"     {m:<5} {t:<6} {c:>10,}")

print("\n=== 2. 代码冲突 ===")
dup = scalar("select count() c from (select ts, code, market, count() k from ohlcv_1d group by ts, code, market) where k > 1")
check("同一标的同一日无重复", dup == 0, f"重复组={dup}")

pairs = scalar("select count() c from (select ts, code, count() k from ohlcv_1d group by ts, code) where k > 1")
print(f"     (ts,code) 同名组 {pairs:,} 组 —— 均为「指数 + 个股」同码，属预期")

mix = rows("select code, market, type, count() c from ohlcv_1d where code='000001' group by code, market, type")
check("000001 指数/个股分开", len(mix) == 2, str(mix))

print("\n=== 3. 新鲜度 ===")
cov = rows("select ts, count() c from ohlcv_1d where ts >= '2026-10-26' group by ts order by ts")
for ts, c in cov:
    print(f"     {str(ts)[:10]}: {c:,}")
last_cnt = dict((str(ts)[:10], c) for ts, c in cov)["2026-10-29"]
check("10-29 已补", last_cnt > 7000, f"{last_cnt} 行")

print("\n=== 4. 2018 回补 ===")
m = rows("select left(to_str(ts,'yyyy-MM'),7) m, count() c from ohlcv_1d "
         "where ts >= '2018-01-01' and ts < '2019-01-01' group by m order by m")
low = [(mm, c) for mm, c in m if c < 40000]
for mm, c in m:
    print(f"     {mm}: {c:,}")
check("2018 每月 > 4 万行", not low, f"偏低月份={low}")

print("\n=== 5. 下游 ===")
for tbl, col in (("fin_valuation_daily", "ts"), ("moneyflow_daily", "ts"), ("factor_daily_wide", "ts")):
    try:
        mx = scalar(f"select max({col}) from {tbl}")
        print(f"     {tbl}: max={str(mx)[:10]}")
    except Exception as e:  # noqa: BLE001
        print(f"     {tbl}: {e}")

print("\n=== 6. 调度 ===")
run = rows("select dataset, status, updated_at from scheduled_task_state where status='RUNNING'")
check("无长期 RUNNING", len(run) == 0, f"{len(run)} 条")

print("\n=== 汇总 ===")
bad = [c for c in checks if not c[1]]
print(f"{len(checks)-len(bad)}/{len(checks)} 通过")
for name, ok, detail in bad:
    print("  FAIL", name, detail)
