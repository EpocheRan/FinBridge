"""修复后的覆盖率复核（按 code+market 分组，避免指数/个股同码重复计数）。"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import requests

BASE = "http://127.0.0.1:9000"


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=300)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype={"code": str})
    for c in ("code", "mn", "mx", "ts"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize() if c != "code" else df[c].astype(str).str.zfill(6)
    return df


cal = q("select ts from trading_days where is_trading = true and ts >= '2001-01-01' and ts <= '2026-10-29'")
cal = cal["ts"].sort_values().reset_index(drop=True)
CAL = cal.to_numpy(dtype="datetime64[D]").astype(np.int64)


def span(first, last):
    lo = np.searchsorted(CAL, first, "left")
    hi = np.searchsorted(CAL, last, "right")
    return np.maximum(hi - lo, 0)


def audit(label: str, sql: str) -> tuple[int, int]:
    agg = q(sql)
    agg["expected"] = span(agg["mn"].to_numpy(dtype="datetime64[D]").astype(np.int64),
                           agg["mx"].to_numpy(dtype="datetime64[D]").astype(np.int64))
    exp, act = int(agg["expected"].sum()), int(agg["c"].sum())
    print(f"{label:<34} 应有 {exp:>11,}  实有 {act:>11,}  缺口 {exp-act:>8,}  完整度 {act/exp*100:5.1f}%")
    return exp, act


print("修复后（按 code+market 分组）")
print("-" * 96)
audit("日线（全口径）", "select code, market, min(ts) mn, max(ts) mx, count() c from ohlcv_1d group by code, market")
audit("日线（个股）", "select code, market, min(ts) mn, max(ts) mx, count() c from ohlcv_1d where type='stock' group by code, market")
audit("资金流", "select code, min(ts) mn, max(ts) mx, count() c from moneyflow_daily group by code")
audit("估值", "select code, min(ts) mn, max(ts) mx, count() c from fin_valuation_daily group by code")

print("\n逐年（全口径）")
print("-" * 96)
real = q("select ts, count() c from ohlcv_1d group by ts order by ts")
real["year"] = real["ts"].dt.year
real_y = real.groupby("year")["c"].sum()

agg = q("select code, market, min(ts) mn, max(ts) mx from ohlcv_1d group by code, market")
first = agg["mn"].to_numpy(dtype="datetime64[D]").astype(np.int64)
last = agg["mx"].to_numpy(dtype="datetime64[D]").astype(np.int64)
print(f"{'年份':<6}{'应有':>12}{'实有':>12}{'缺口':>12}{'完整度':>9}")
for y in range(2018, 2027):
    idx = np.where((cal.dt.year == y).to_numpy())[0]
    if len(idx) == 0:
        continue
    ys, ye = CAL[idx[0]], CAL[idx[-1]]
    lo, hi = np.maximum(first, ys), np.minimum(last, ye)
    ok = lo <= hi
    exp = int(span(lo[ok], hi[ok]).sum())
    act = int(real_y.get(y, 0))
    print(f"{y:<6}{exp:>12,}{act:>12,}{exp-act:>12,}{act/exp*100:>8.1f}%")
