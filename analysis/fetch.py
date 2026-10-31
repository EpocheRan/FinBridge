"""Fetch market data needed for the 2d/5d/14d probability-matrix study.

Sources: local QuestDB (ohlcv_1d, moneyflow_daily, margin_detail, top_list,
fin_valuation_daily, fin_indicator) + Tushare HTTP API for the last session,
which the local sync has not picked up yet (2026-10-29).
"""
from __future__ import annotations

import io
import json
import os
import time

import pandas as pd
import requests

QDB = "http://127.0.0.1:9000"
TS_TOKEN = "c2b88929f526214c69807fcff84043999c7539fba1f9837f5c7bb129"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)

TARGETS = {"000949": "新乡化纤", "600418": "江淮汽车"}


def qdb(sql: str, timeout: int = 300) -> pd.DataFrame:
    r = requests.get(f"{QDB}/exp", params={"query": sql}, timeout=timeout)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype={"code": str, "market": str})
    if "code" in df.columns:
        df["code"] = df["code"].str.zfill(6)
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return df


def tushare(api: str, params: dict, fields: str = "") -> pd.DataFrame:
    body = {"api_name": api, "token": TS_TOKEN, "params": params, "fields": fields}
    r = requests.post("http://api.tushare.pro", json=body, timeout=60)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != 0:
        raise RuntimeError(f"tushare error: {js}")
    d = js["data"]
    return pd.DataFrame(d["items"], columns=d["fields"])


def ts_code(code: str) -> str:
    return f"{code}.SZ" if code.startswith(("0", "3")) else f"{code}.SH"


def last_sessions(code: str, start: str = "20261020") -> pd.DataFrame:
    df = tushare("daily", {"ts_code": ts_code(code), "start_date": start, "end_date": "20261030"},
                 "trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df["volume"] = df["vol"] * 100.0          # tushare vol is 手 -> shares
    df["amount"] = df["amount"] * 1000.0      # 千元 -> 元
    return df[["ts", "open", "high", "low", "close", "volume", "amount"]]


def main() -> None:
    t0 = time.time()

    # ---------- 1. target stocks, full history ----------
    for code, name in TARGETS.items():
        df = qdb(
            "select ts,open,high,low,close,volume,amount,turnover,adj_factor "
            f"from ohlcv_1d where code='{code}' and ts>='2016-01-01' order by ts"
        )
        tail = last_sessions(code)
        if not tail.empty:
            tail["turnover"] = None
            tail["adj_factor"] = df["adj_factor"].iloc[-1]
            df = pd.concat([df[df["ts"] < tail["ts"].min()], tail], ignore_index=True)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        df.to_csv(os.path.join(DATA, f"bars_{code}.csv"), index=False)
        print(f"bars {code} {name}: {len(df)} rows, {df['ts'].min().date()} -> {df['ts'].max().date()}")

        # flows / margin / 龙虎榜 / valuation / indicators
        qdb(f"select * from moneyflow_daily where code='{code}' order by ts").to_csv(
            os.path.join(DATA, f"moneyflow_{code}.csv"), index=False)
        qdb(f"select * from margin_detail where code='{code}' order by ts").to_csv(
            os.path.join(DATA, f"margin_{code}.csv"), index=False)
        qdb(f"select * from top_list where code='{code}' order by ts").to_csv(
            os.path.join(DATA, f"toplist_{code}.csv"), index=False)
        qdb("select ts,close,market_cap,float_market_cap,pe_ttm,pb_mrq,ps_ttm,dividend_yield "
            f"from fin_valuation_daily where code='{code}' and ts>='2019-01-01' order by ts").to_csv(
            os.path.join(DATA, f"valuation_{code}.csv"), index=False)
        qdb("select period_end,fiscal_year,period_type,revenue,net_income,gross_margin,net_margin,roe,roa,"
            "debt_to_assets,current_ratio,ocf,revenue_yoy,net_income_yoy "
            f"from fin_indicator where code='{code}' order by period_end").to_csv(
            os.path.join(DATA, f"indicator_{code}.csv"), index=False)
        qdb("select period_end,publish_date,total_revenue,revenue,gross_profit,operating_profit,"
            "net_income,net_income_attributable_to_parent,basic_eps "
            f"from fin_income_statement where code='{code}' order by period_end").to_csv(
            os.path.join(DATA, f"income_{code}.csv"), index=False)

    # ---------- 2. benchmark ----------
    idx = qdb("select ts,open,high,low,close,volume,amount from ohlcv_1d "
              "where code='000300' order by ts")
    ix_tail = tushare("index_daily", {"ts_code": "000300.SH", "start_date": "20261020", "end_date": "20261030"},
                      "trade_date,open,high,low,close,vol,amount")
    if not ix_tail.empty:
        ix_tail["ts"] = pd.to_datetime(ix_tail["trade_date"], format="%Y%m%d")
        ix_tail = ix_tail.rename(columns={"vol": "volume"})
        ix_tail = ix_tail[["ts", "open", "high", "low", "close", "volume", "amount"]]
        ix_tail["volume"] = pd.to_numeric(ix_tail["volume"], errors="coerce")
        ix_tail["amount"] = pd.to_numeric(ix_tail["amount"], errors="coerce")
        idx = pd.concat([idx[idx["ts"] < pd.Timestamp("2026-10-20")], ix_tail], ignore_index=True)
    idx = idx.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    idx.to_csv(os.path.join(DATA, "bars_index.csv"), index=False)
    print(f"index: {len(idx)} rows, last {idx['ts'].max().date()}")

    # ---------- 3. cross-sectional sample for conditional base rates ----------
    uni = qdb("select code, count() c from ohlcv_1d where ts between '2026-01-01' and '2026-10-28' group by code")
    uni = uni[uni["c"] >= 100]
    codes = sorted(uni["code"].astype(str).tolist())
    codes = [c for c in codes if c[:1] in ("0", "3", "6") and len(c) == 6]
    rng = pd.Series(codes).sample(frac=1.0, random_state=20261030).tolist()
    pick = rng[:1400]
    for c in TARGETS:
        if c not in pick:
            pick.append(c)
    print(f"universe: {len(codes)} listed, sampling {len(pick)}")

    frames = []
    chunk = 140
    for i in range(0, len(pick), chunk):
        part = pick[i:i + chunk]
        inlist = ",".join(f"'{c}'" for c in part)
        sql = ("select ts,code,open,high,low,close,volume,amount,turnover,adj_factor "
               f"from ohlcv_1d where code in ({inlist}) and ts>='2020-01-01' order by code,ts")
        frames.append(qdb(sql))
        print(f"  chunk {i//chunk+1}: {len(frames[-1])} rows ({time.time()-t0:.0f}s)", flush=True)

    # append the missing 2026-10-29 session for the two names we care about
    tail_rows = []
    for code in TARGETS:
        tail = last_sessions(code, start="20261029")
        if not tail.empty:
            tail = tail[tail["ts"] == pd.Timestamp("2026-10-29")].copy()
            tail["code"] = code
            tail["turnover"] = None
            tail_rows.append(tail)
    pooled = pd.concat(frames, ignore_index=True)
    if tail_rows:
        pooled = pd.concat([pooled] + tail_rows, ignore_index=True)
    pooled = pooled.drop_duplicates(["code", "ts"]).sort_values(["code", "ts"]).reset_index(drop=True)
    pooled.to_csv(os.path.join(DATA, "pooled_daily.csv"), index=False)
    print(f"pooled: {len(pooled)} rows, {pooled['code'].nunique()} codes ({time.time()-t0:.0f}s)")

    with open(os.path.join(DATA, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"universe": len(codes), "sampled": len(pick), "targets": TARGETS,
                   "fetched_at": pd.Timestamp.now().isoformat()}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
