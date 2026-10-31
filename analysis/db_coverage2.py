"""补充审计：个股口径的覆盖率、2018 年逐月缺口、近 120 日代码级缺口。"""
from __future__ import annotations

import io
import os

import numpy as np
import pandas as pd
import requests

BASE = "http://127.0.0.1:9000"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=300)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype={"code": str})
    if "code" in df.columns:
        df["code"] = df["code"].str.zfill(6)
    for c in ("ts", "period_end", "mn", "mx"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    return df


def d2i(s) -> np.ndarray:
    return pd.to_datetime(s).to_numpy(dtype="datetime64[D]").astype(np.int64)


cal = q("select ts from trading_days where is_trading = true and ts >= '2001-01-01' and ts <= '2026-10-29'")
cal = cal["ts"].sort_values().reset_index(drop=True)
CAL = d2i(cal)

stocks = q("select distinct code from stock_basic_info where security_type = 'STOCK'")["code"]
print(f"个股池（security_type=STOCK）: {len(stocks)} 只")

print("\n【个股口径：面板表应有 vs 实有】")
print(f"{'表':<24}{'应有':>12}{'实有':>12}{'缺口':>10}{'完整度':>8}{'标的':>7}")
tot = []
for tbl in ("ohlcv_1d", "moneyflow_daily", "margin_detail", "fin_valuation_daily"):
    agg = q(f"select code, min(ts) mn, max(ts) mx, count() c from {tbl} group by code")
    agg = agg[agg["code"].isin(stocks)]
    lo, hi = d2i(agg["mn"]), d2i(agg["mx"])
    exp = int(np.maximum(np.searchsorted(CAL, hi, "right") - np.searchsorted(CAL, lo, "left"), 0).sum())
    act = int(agg["c"].sum())
    print(f"{tbl:<24}{exp:>12,}{act:>12,}{exp-act:>10,}{act/exp*100:>7.1f}%{len(agg):>7}")
    tot.append({"table": tbl, "expected": exp, "actual": act, "missing": exp - act, "pct": round(act / exp * 100, 1)})

print("\n【日线 2018 年逐月（个股口径）】")
d18 = q("select ts, count() c from ohlcv_1d where ts>='2018-01-01' and ts<'2019-01-01' group by ts order by ts")
d18["m"] = d18["ts"].dt.strftime("%Y-%m")
m18 = d18.groupby("m").agg(days=("ts", "count"), rows=("c", "sum"), avg=("c", "mean")).round(0)
print(m18.to_string())

print("\n【近 120 个交易日代码级缺口（个股，窗口截至 10-28）】")
win = cal[(cal >= pd.Timestamp("2026-05-01")) & (cal <= pd.Timestamp("2026-10-28"))]
n_win = len(win)
cnt = q("select code, count() c from ohlcv_1d where ts>='2026-05-01' and ts<='2026-10-28' group by code")
cnt = cnt[cnt["code"].isin(stocks)].copy()
cnt["miss"] = n_win - cnt["c"]
bins = pd.cut(cnt["miss"], [-1, 0, 3, 10, 30, 10000], labels=["0 天", "1-3 天", "4-10 天", "11-30 天", ">30 天"])
print(f"  窗口 {win.iloc[0].date()} ~ {win.iloc[-1].date()}，共 {n_win} 个交易日；个股 {len(cnt)} 只")
print("  分布:", {k: int(v) for k, v in bins.value_counts().reindex(["0 天", "1-3 天", "4-10 天", "11-30 天", ">30 天"]).items()})
bad = cnt[cnt["miss"] > 10].sort_values("miss", ascending=False)
print(f"  缺口 >10 天的个股: {len(bad)} 只，样例:")
print(bad.head(10)[["code", "c", "miss"]].to_string(index=False))

print("\n【缺口的日分布：最近 250 个交易日中，每个交易日缺多少只个股】")
recent = CAL[CAL >= CAL[-260]]
rows = q("select ts, count() c from ohlcv_1d where ts>='2025-10-01' group by ts order by ts")
rows = rows[rows["ts"] >= pd.Timestamp("2025-10-01")]
full = len(cnt)  # 当前个股数，仅作参考
rows["gap"] = full - rows["c"]
worst = rows.nsmallest(8, "c")
print(worst.assign(ts=worst["ts"].dt.strftime("%Y-%m-%d"))[["ts", "c"]].to_string(index=False))

pd.DataFrame(tot).to_csv(os.path.join(OUT, "coverage_stocks.csv"), index=False)
print("\n已写出 analysis/out/coverage_stocks.csv")
