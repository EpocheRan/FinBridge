"""按代码分批重建因子（宿主机内存受限时使用）。

读取最近一次 factor_build_state 的 factor_ids，按 400 只一批调用
FactorEngineService.build()，逐批写入 factor_value_daily / factor_value_latest。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pandas as pd
import requests

BASE = "http://127.0.0.1:9000"
BATCH = int(os.getenv("FACTOR_BATCH", "400"))
START = os.getenv("FACTOR_START", "2026-10-29")
END = os.getenv("FACTOR_END", "2026-10-29")


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=120)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), dtype={"code": str})


row = q("select factor_ids from factor_build_state where status='DONE' order by updated_at desc limit 1")["factor_ids"].iloc[0]
ids = [x.strip() for x in str(row).split(",") if x.strip()]
print(f"factor ids: {len(ids)}")

codes = q(f"select distinct code from ohlcv_1d where ts='{END}' and type <> 'idx'")["code"].astype(str).tolist()
_d = q(f"select code, count() c from factor_value_latest where ts='{END}' group by code")
done = set(_d.loc[_d["c"] >= 55, "code"].astype(str))
codes = sorted(c for c in codes if c not in done)
print(f"already done: {len(done)}, remaining: {len(codes)}")
print(f"codes: {len(codes)}, batch={BATCH}")

sys.path.insert(0, r"D:\FinBridge")
from factor_engine.service import FactorEngineService  # noqa: E402

service = FactorEngineService()
total = 0
t0 = time.time()
for i in range(0, len(codes), BATCH):
    part = codes[i:i + BATCH]
    try:
        res = service.build(ids, START, END, part)
        n = int(res.get("rows_written") or 0)
        total += n
        print(f"  batch {i//BATCH+1}/{(len(codes)+BATCH-1)//BATCH}: {len(part)} codes -> {n} rows "
              f"(cum {total:,}, {time.time()-t0:.0f}s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  batch {i//BATCH+1} FAILED: {str(exc)[:120]}", flush=True)

print(f"done: {total:,} rows in {time.time()-t0:.0f}s")
