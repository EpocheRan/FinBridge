"""数据覆盖率审计：应有条数 vs 实有条数（按表 / 按年 / 按报告期 / 按代码）。

两类表分开算，避免用错的尺子：
1) 面板型（每标的每交易日一行）：ohlcv_1d / moneyflow_daily / margin_detail /
   fin_valuation_daily / concept_board_daily —— "应有"= 每个标的存续窗口内的交易日数。
2) 事件型（有事件才有行）：top_list / block_trade / repurchase / stk_holdernumber /
   share_float / announcement —— 不算"应有行数"，改用"最近一日覆盖 + 与 Tushare 抽样对比"。
"""
from __future__ import annotations

import io
import os

import numpy as np
import pandas as pd
import requests

BASE = "http://127.0.0.1:9000"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=300)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype={"code": str, "board_code": str})
    for c in ("code", "board_code"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.zfill(6)
    for c in ("ts", "period_end", "mn", "mx"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    return df


def d2i(s) -> np.ndarray:
    return pd.to_datetime(s).to_numpy(dtype="datetime64[D]").astype(np.int64)


cal = q("select ts from trading_days where is_trading = true and ts >= '2001-01-01' and ts <= '2026-10-29'")
cal = cal["ts"].sort_values().reset_index(drop=True)
CAL = d2i(cal)
print(f"交易日历 {len(cal)} 天（{cal.iloc[0].date()} ~ {cal.iloc[-1].date()}），基准日 2026-10-28")


def span_days(first: np.ndarray, last: np.ndarray) -> np.ndarray:
    lo = np.searchsorted(CAL, first, side="left")
    hi = np.searchsorted(CAL, last, side="right")
    return np.maximum(hi - lo, 0)


def audit(table: str, label: str, key: str = "code") -> dict:
    agg = q(f"select {key} k, min(ts) mn, max(ts) mx, count() c from {table} group by {key}")
    if agg.empty:
        print(f"{label}: 空表")
        return {}
    agg["expected"] = span_days(d2i(agg["mn"]), d2i(agg["mx"]))
    exp, act = int(agg["expected"].sum()), int(agg["c"].sum())
    pct = act / exp * 100 if exp else 0
    print(f"{label:<26} 应有 {exp:>10,}  实有 {act:>10,}  缺口 {exp-act:>9,}  完整度 {pct:5.1f}%  标的 {len(agg):>5}")
    return {"table": table, "label": label, "expected": exp, "actual": act,
            "codes": len(agg), "missing": exp - act, "pct": round(pct, 1), "detail": agg}


print("\n" + "=" * 100)
print("一、面板型表：应有 / 实有")
print("=" * 100)
rows, details = [], {}
PANEL = [("ohlcv_1d", "日线 ohlcv_1d", "code"), ("moneyflow_daily", "资金流 moneyflow_daily", "code"),
         ("margin_detail", "两融 margin_detail", "code"), ("fin_valuation_daily", "估值 fin_valuation_daily", "code"),
         ("concept_board_daily", "板块 concept_board_daily", "board_code")]
for tbl, label, key in PANEL:
    res = audit(tbl, label, key)
    if res:
        details[tbl] = res.pop("detail")
        rows.append(res)

print("\n" + "=" * 100)
print("二、日线逐年：应有 vs 实有")
print("=" * 100)
agg = details["ohlcv_1d"]
first, last = d2i(agg["mn"]), d2i(agg["mx"])
real = q("select ts, count() c from ohlcv_1d where ts>='2018-01-01' group by ts")
real["year"] = real["ts"].dt.year
real_y = real.groupby("year")["c"].sum()

print(f"{'年份':<6}{'应有':>12}{'实有':>12}{'缺口':>12}{'完整度':>9}   说明")
year_rows = []
for y in range(2018, 2027):
    idx = np.where((cal.dt.year == y).to_numpy())[0]
    if len(idx) == 0:
        continue
    ys, ye = CAL[idx[0]], CAL[idx[-1]]
    lo, hi = np.maximum(first, ys), np.minimum(last, ye)
    ok = lo <= hi
    exp = int(span_days(lo[ok], hi[ok]).sum())
    act = int(real_y.get(y, 0))
    note = "截至 10-28（10-29 未同步）" if y == 2026 else ""
    print(f"{y:<6}{exp:>12,}{act:>12,}{exp-act:>12,}{act/exp*100:>8.1f}%   {note}")
    year_rows.append({"year": y, "expected": exp, "actual": act, "missing": exp - act, "pct": round(act / exp * 100, 1)})

print("\n  各年缺口占全部缺口的比例:")
tot_missing = sum(r["missing"] for r in year_rows)
for r in year_rows:
    if r["missing"] > 0:
        print(f"    {r['year']}: 缺 {r['missing']:>8,}  占 {r['missing']/tot_missing*100:5.1f}%")

print("\n" + "=" * 100)
print("三、财务表按报告期覆盖（distinct 公司数）")
print("=" * 100)
for tbl in ("fin_income_statement", "fin_balance_sheet", "fin_cash_flow", "fin_indicator"):
    df = q(f"select period_end, count_distinct(code) n from {tbl} where period_end>='2024-03-31' group by period_end order by period_end")
    txt = "  ".join(f"{str(r.period_end)[:10]}:{int(r.n)}" for r in df.itertuples())
    print(f"  {tbl:<24} {txt}")

print("\n" + "=" * 100)
print("四、最近交易日各表覆盖（行数）")
print("=" * 100)
for tbl in ("ohlcv_1d", "fin_valuation_daily", "moneyflow_daily", "margin_detail", "top_list", "block_trade"):
    df = q(f"select ts, count() c from {tbl} where ts>='2026-10-20' group by ts order by ts")
    s = " ".join(f"{str(r.ts)[5:10]}:{int(r.c)}" for r in df.itertuples())
    print(f"  {tbl:<22} {s}")

print("\n" + "=" * 100)
print("五、事件型表：与 Tushare 抽样核对（10-28）")
print("=" * 100)
TS = "c2b88929f526214c69807fcff84043999c7539fba1f9837f5c7bb129"


def ts_count(api: str, date: str) -> int:
    body = {"api_name": api, "token": TS, "params": {"trade_date": date}, "fields": "ts_code"}
    r = requests.post("http://api.tushare.pro", json=body, timeout=40).json()
    return len(r["data"]["items"]) if r.get("code") == 0 else -1


for api, tbl in (("top_list", "top_list"), ("block_trade", "block_trade")):
    try:
        n_ts = ts_count(api, "20261028")
        n_db = int(q(f"select count() n from {tbl} where ts='2026-10-28'")["n"].iloc[0])
        print(f"  {tbl:<14} Tushare {n_ts:>5} 条 / 库内 {n_db:>5} 条")
    except Exception as e:  # noqa: BLE001
        print(f"  {tbl}: 对比失败 {e}")

ann = q("select count() rows, count_distinct(code) n, count_distinct(code) null_code from announcement")
print(f"  announcement   {int(ann['rows'].iloc[0]):>5} 行 / {int(ann['n'].iloc[0])} 只（全市场 5,918，覆盖 {int(ann['n'].iloc[0])/5918*100:.1f}%）")
for tbl in ("repurchase", "stk_holdernumber", "share_float"):
    n = int(q(f"select count() c from {tbl}")["c"].iloc[0])
    last = q(f"select max(ts) m from {tbl}")["m"].iloc[0]
    print(f"  {tbl:<14} {n:>7,} 行，最后 {str(last)[:10]}")

print("\n" + "=" * 100)
print("六、代码级缺口（活跃标的近 120 个交易日）")
print("=" * 100)
win = cal[cal >= pd.Timestamp("2026-05-01")]
n_win = len(win)
print(f"  窗口 {win.iloc[0].date()} ~ {win.iloc[-1].date()}，共 {n_win} 个交易日")
cnt = q("select code, count() c from ohlcv_1d where ts>='2026-05-01' and ts<='2026-10-28' group by code")
active = q("select distinct code from ohlcv_1d where ts='2026-10-28'")["code"]
cnt = cnt[cnt["code"].isin(active)].copy()
cnt["miss"] = n_win - cnt["c"]
bins = pd.cut(cnt["miss"], [-1, 0, 5, 15, 40, 10000], labels=["0", "1-5", "6-15", "16-40", ">40"])
print("  缺失分布:", {k: int(v) for k, v in bins.value_counts().reindex(["0", "1-5", "6-15", "16-40", ">40"]).items()})
print("  缺口最大 10 只:")
print(cnt.nlargest(10, "miss")[["code", "c", "miss"]].to_string(index=False))

print("\n" + "=" * 100)
print("七、代码命名空间冲突")
print("=" * 100)
dup = q("select count() c from (select ts, code, count() k from ohlcv_1d where ts>='2026-01-01' group by ts, code) where k>1")
print(f"  2026 年内 (ts,code) 同时存在指数与个股的组数: {int(dup['c'].iloc[0])}")
print("  000001 上证指数/平安银行 | 000016 上证50/深康佳A | 000688 科创50/国城矿业")
print("  000852 中证1000/石化机械 | 000905 中证500/厦门港务")

pd.DataFrame(rows).to_csv(os.path.join(OUT, "coverage_by_table.csv"), index=False)
pd.DataFrame(year_rows).to_csv(os.path.join(OUT, "coverage_by_year.csv"), index=False)
print("\n已写出 analysis/out/coverage_by_table.csv, coverage_by_year.csv")
