"""QuestDB 数据健康巡检：新鲜度、覆盖率、质量、任务状态。"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime

import pandas as pd
import requests

BASE = "http://127.0.0.1:9000"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)
NOW = datetime(2026, 10, 30, 11, 40)
TODAY = pd.Timestamp("2026-10-30")
LAST_SESSION = pd.Timestamp("2026-10-29")


def q(sql: str) -> pd.DataFrame:
    r = requests.get(f"{BASE}/exp", params={"query": sql}, timeout=300)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype={"code": str, "table_name": str})
    if "code" in df.columns:
        df["code"] = df["code"].str.zfill(6)
    for c in df.columns:
        if c in ("ts", "table_min_timestamp", "table_max_timestamp", "table_last_write_timestamp",
                 "updated_at", "period_end", "publish_date", "last"):
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return df


def section(t: str) -> None:
    print(f"\n{'='*78}\n{t}\n{'='*78}")


issues: list[dict] = []


def flag(level: str, area: str, msg: str) -> None:
    issues.append({"level": level, "area": area, "msg": msg})
    print(f"  [{level}] {area}: {msg}")


# ---------------------------------------------------------------- 1. server
section("1. 服务与实例")
try:
    r = requests.get(f"{BASE}/exec", params={"query": "select 1"}, timeout=10)
    print("  活跃性: /exec OK", r.status_code)
except Exception as e:  # noqa: BLE001
    flag("P0", "server", f"/exec 不可用: {e}")

tables = q("select table_name, table_row_count, table_max_timestamp, table_last_write_timestamp, "
           "table_suspended, table_memory_pressure_level, wal_pending_row_count "
           "from tables() order by table_name")
print(f"  表数量: {len(tables)}")
empty = tables[tables["table_row_count"] == 0]
print(f"  空表: {len(empty)} -> {', '.join(empty['table_name'])}")
susp = tables[tables["table_suspended"].astype(str).str.lower() == "true"]
if len(susp):
    flag("P0", "storage", f"挂起的表: {list(susp['table_name'])}")
pend = tables[pd.to_numeric(tables["wal_pending_row_count"], errors="coerce").fillna(0) > 0]
if len(pend):
    flag("P1", "storage", f"WAL 未提交: {len(pend)} 张表")

# ------------------------------------------------------- 2. ohlcv freshness
section("2. 日线新鲜度与覆盖")
daily = q("select ts, count() c, count_distinct(code) n from ohlcv_1d where ts>='2026-09-25' group by ts order by ts")
daily["weekday"] = daily["ts"].dt.day_name()
show = daily.tail(16).copy()
show["ts"] = show["ts"].dt.strftime("%Y-%m-%d")
print(show.to_string(index=False))
last = daily.iloc[-1]
prev = daily.iloc[-2]
if last["c"] < 1000:
    flag("P0", "ohlcv_1d", f"最新交易日 {last['ts'].date()} 只有 {int(last['c'])} 行（前一交易日 {int(prev['c'])} 行）→ 当日行情未同步")
if prev["ts"] != LAST_SESSION:
    flag("P1", "ohlcv_1d", f"最后一个完整交易日是 {prev['ts'].date()}，不是 {LAST_SESSION.date()}")

cal = q("select ts, is_trading from trading_days where ts>='2026-10-20' and ts<='2026-11-05' order by ts")
print("\n  交易日历:")
print(cal.to_string(index=False))
trading_days = cal[cal["is_trading"] == True]  # noqa: E712
have = set(daily["ts"])
missing = [d for d in trading_days["ts"] if d not in have and d <= LAST_SESSION]
if missing:
    flag("P0", "ohlcv_1d", f"交易日历内的缺失交易日: {[str(d.date()) for d in missing]}")

codes_last = q("select count_distinct(code) n from ohlcv_1d where ts='2026-10-28'")
print(f"\n  10-28 覆盖股票数: {int(codes_last['n'].iloc[0])}")

# 每只票最后交易日分布（判断是否有票长期断更）
stale = q("select days_since, count() n from (select code, datediff('d', max(ts), '2026-10-29') days_since "
          "from ohlcv_1d group by code) where days_since > 3 group by days_since order by days_since")
print("  距 10-29 超过 3 天没更新的代码数（按天数）:")
print(stale.head(10).to_string(index=False) if len(stale) else "    （无）")

# ------------------------------------------------------------ 3. quality
section("3. 最近 5 个交易日的字段质量")
qual = q("""
select ts,
  count() n,
  sum(case when adj_factor is null then 1 else 0 end) null_adj,
  sum(case when volume <= 0 then 1 else 0 end) bad_vol,
  sum(case when high < low then 1 else 0 end) hi_lt_lo,
  sum(case when close > high or close < low then 1 else 0 end) close_out,
  sum(case when open <= 0 or close <= 0 then 1 else 0 end) nonpos
from ohlcv_1d where ts >= '2026-10-20' group by ts order by ts
""")
qual["null_adj_pct"] = (qual["null_adj"] / qual["n"] * 100).round(2)
qual["bad_vol_pct"] = (qual["bad_vol"] / qual["n"] * 100).round(2)
print(qual[["ts", "n", "null_adj_pct", "bad_vol_pct", "hi_lt_lo", "close_out", "nonpos"]].to_string(index=False))
if (qual["null_adj_pct"].fillna(0) > 5).any():
    flag("P1", "ohlcv_1d", "复权因子缺失比例超过 5%，前复权价可能失真")

dup = q("select count() c from (select ts, code, count() k from ohlcv_1d where ts>='2026-10-20' group by ts, code) where k > 1")
print(f"  重复主键 (ts,code): {int(dup['c'].iloc[0])}")

# --------------------------------------------------- 4. derived table clock
section("4. 派生表/业务表新鲜度")
expect = {
    "ohlcv_1d": LAST_SESSION, "fin_valuation_daily": LAST_SESSION, "moneyflow_daily": pd.Timestamp("2026-10-28"),
    "margin_detail": pd.Timestamp("2026-10-28"), "top_list": LAST_SESSION, "announcement": LAST_SESSION,
    "concept_board_daily": pd.Timestamp("2026-10-28"), "factor_value_latest": LAST_SESSION,
    "trading_days": LAST_SESSION, "stock_basic_info": LAST_SESSION,
}
for name, exp in expect.items():
    row = tables[tables["table_name"] == name]
    if row.empty:
        continue
    mx = row["table_max_timestamp"].iloc[0]
    lag = (exp - mx).days if pd.notna(mx) else None
    mark = "OK" if pd.notna(mx) and mx >= exp else f"落后 {lag} 天"
    print(f"  {name:<22} max={str(mx)[:10] if pd.notna(mx) else 'None':<12} 期望>={exp.date()}  {mark}")
    if pd.notna(mx) and mx < exp:
        lvl = "P0" if name in ("ohlcv_1d",) else "P2"
        flag(lvl, name, f"最新数据 {str(mx)[:10]}，落后于预期 {exp.date()}")

# 估值/资金/两融覆盖率（当日有值的股票数）
cov = q("""
select 'valuation' t, count_distinct(code) n from fin_valuation_daily where ts='2026-10-29'
union all select 'moneyflow', count_distinct(code) from moneyflow_daily where ts='2026-10-28'
union all select 'margin', count_distinct(code) from margin_detail where ts='2026-10-28'
union all select 'factor_latest', count_distinct(code) from factor_value_latest where ts='2026-10-29'
union all select 'factor_latest_28', count_distinct(code) from factor_value_latest where ts='2026-10-28'
""")
print("\n  覆盖率（当日有记录的代码数）:")
print(cov.to_string(index=False))

# ------------------------------------------------------------ 5. financials
section("5. 财务与披露")
per = q("select period_end, count() rows, count_distinct(code) n from fin_income_statement "
        "where period_end>='2026-03-31' group by period_end order by period_end")
show = per.copy()
show["period_end"] = show["period_end"].dt.strftime("%Y-%m-%d")
print(show.to_string(index=False))
q3mask = per["period_end"] == pd.Timestamp("2026-09-30 08:00")
q3 = int(per.loc[q3mask, "n"].iloc[0]) if q3mask.any() else 0
print(f"  已入库 2026Q3 公司数: {q3}（A 股约 5,600 家，三季报截止 10-31）")

dis = q("select count() rows, count_distinct(code) n, max(ts) last from fin_disclosure_calendar")
print(f"  披露日历: {int(dis['rows'].iloc[0])} 行 / {int(dis['n'].iloc[0])} 只，最后更新 {dis['last'].iloc[0]}")
if int(dis["n"].iloc[0]) < 3000:
    flag("P1", "fin_disclosure_calendar", f"只覆盖 {int(dis['n'].iloc[0])} 只股票，预约披露日缺失会导致'事件窗口'判断失真")

ann = q("select count() rows, count_distinct(code) n, max(ts) last from announcement")
print(f"  公告: {int(ann['rows'].iloc[0])} 行 / {int(ann['n'].iloc[0])} 只，最后 {ann['last'].iloc[0]}")
if int(ann["n"].iloc[0]) < 1000:
    flag("P1", "announcement", f"公告只覆盖 {int(ann['n'].iloc[0])} 只股票（全市场约 5,900），600418 等热门票查不到公告")

fs = q("select status, count() c from fin_sync_state group by status")
print("  fin_sync_state 状态分布:")
print(fs.to_string(index=False))

# ---------------------------------------------------------------- 6. tasks
section("6. 调度任务")
tasks = q("select task_name, dataset, status, processed, total, updated_at, message from scheduled_task_state "
          "order by updated_at desc limit 40")
last_by_ds = tasks.drop_duplicates(subset=["dataset"], keep="first")
print(last_by_ds[["dataset", "status", "processed", "total", "updated_at"]].to_string(index=False))
running = tasks[tasks["status"] == "RUNNING"]
old = running[(NOW - running["updated_at"]).dt.total_seconds() > 3600 * 2]
if len(old):
    for _, r in old.iterrows():
        flag("P1", "scheduler", f"{r['dataset']} 任务自 {r['updated_at']} 起处于 RUNNING（已 {(NOW - r['updated_at']).total_seconds()/3600:.1f} 小时），疑似僵尸任务: {str(r['message'])[:60]}")

# ------------------------------------------------------------ 7. benchmark
section("7. 基准与次要表")
idx = q("select code, max(ts) last, count() c from ohlcv_1d where code in ('000300','000001','399001') group by code")
print(idx.to_string(index=False))
for _, r in idx.iterrows():
    if pd.notna(r["last"]) and r["last"] < LAST_SESSION:
        flag("P1", "ohlcv_1d", f"基准 {r['code']} 最新只到 {r['last'].date()}")

minute = q("select count() rows, count_distinct(code) n, max(ts) last from ohlcv_5m")
print(f"  5 分钟线: {int(minute['rows'].iloc[0])} 行 / {int(minute['n'].iloc[0])} 只，最后 {minute['last'].iloc[0]}")
if int(minute["n"].iloc[0]) < 100:
    flag("P2", "ohlcv_5m", "分钟线基本为空（<100 只），日内结构分析不可用")

# ---------------------------------------------------------------- summary
section("汇总")
order = {"P0": 0, "P1": 1, "P2": 2}
issues.sort(key=lambda x: order.get(x["level"], 9))
for i in issues:
    print(f"  {i['level']}  {i['area']:<24} {i['msg']}")
print(f"\n  合计 {len(issues)} 项：P0={sum(1 for i in issues if i['level']=='P0')} "
      f"P1={sum(1 for i in issues if i['level']=='P1')} P2={sum(1 for i in issues if i['level']=='P2')}")

with open(os.path.join(OUT, "db_health.json"), "w", encoding="utf-8") as f:
    json.dump({"checked_at": NOW.isoformat(), "issues": issues,
               "empty_tables": list(empty["table_name"]),
               "daily_coverage": daily.assign(ts=daily["ts"].astype(str)).to_dict("records")}, f,
              ensure_ascii=False, indent=2)
print("\n  结果已写入 analysis/out/db_health.json")
