"""基金（ETF/LOF/fund）价格类因子重建：使用 10-28 出现过的 legacy 因子集合。"""
from __future__ import annotations

import io
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, r"D:\FinBridge")

BASE = "http://127.0.0.1:9000"
END = "2026-10-29"
BATCH = int(os.getenv("FACTOR_BATCH", "300"))

LEGACY_IDS = [
    "momentum.ret", "reversal.short", "risk.vol", "technical.atr", "technical.atr_pct",
    "technical.beta_252", "technical.close_vs_ma20_pct", "technical.close_vs_ma250_pct",
    "technical.close_vs_ma60_pct", "technical.corr_252", "technical.dist_high_52w_pct",
    "technical.dist_low_52w_pct", "technical.gap_pct", "technical.intraday_pct",
    "technical.is_new_high_52w", "technical.is_new_low_52w", "technical.lookback_days",
    "technical.pct_chg", "technical.range_pct", "technical.volatility_20d",
    "technical.volatility_60d", "technical.volume_ratio_20d",
]


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=120)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), dtype={"code": str})


codes = q(f"select distinct code from ohlcv_1d where ts='{END}' and type in ('etf','lof','fund')")["code"].tolist()
done = set(q(f"select distinct code from factor_value_latest where ts='{END}'")["code"].astype(str))
codes = sorted(c for c in codes if c not in done)
print(f"fund codes: {len(codes)}, batch={BATCH}, ids={len(LEGACY_IDS)}")

from factor_engine.service import FactorEngineService  # noqa: E402

service = FactorEngineService()
total, t0 = 0, time.time()
for i in range(0, len(codes), BATCH):
    part = codes[i:i + BATCH]
    try:
        res = service.build(LEGACY_IDS, END, END, part)
        n = int(res.get("rows_written") or 0)
        total += n
        print(f"  batch {i//BATCH+1}/{(len(codes)+BATCH-1)//BATCH}: {len(part)} -> {n} (cum {total:,}, {time.time()-t0:.0f}s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  batch {i//BATCH+1} FAILED: {str(exc)[:100]}", flush=True)
print(f"done: {total:,} rows in {time.time()-t0:.0f}s")
