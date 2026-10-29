#!/usr/bin/env python
"""反转信号 × 信号密度：跨市场状态（2018/2019/2020/2021/2022/…）验证。

回答的问题
----------
密度闸门在 2022-2024 样本上成立，但它是不是只在“那一轮”有效？
本脚本把 2017-11 至今切成若干段（含 2018 贸易战熊市、2020 疫情崩盘、2022 熊市），
每段分别跑「不加闸门 vs 加闸门」，看 edge 是否跨状态稳定。

口径
----
- 股票池：``--pool-date`` 当天的成交额前 N（点位池，避免用未来流动性选历史股票）；
- 信号日 T 收盘出信号，**T+1 开盘价买入**，持有 ``hold`` 日，等权、最多 ``top`` 只、双边 0.2%；
- 密度 = 当日触发信号的股票数。

用法::

    python scripts/research_reversal_regimes.py
    python scripts/research_reversal_regimes.py --pool-date 2017-11-01 --thresholds 1,3,5,6
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402

logger = logging.getLogger("Research.Regimes")
COST = 0.002

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"

SEGMENTS = [
    ("2018 贸易战熊市", "2018-01-01", "2018-12-31"),
    ("2019 修复牛", "2019-01-01", "2019-12-31"),
    ("2020 疫情崩盘+反转", "2020-01-01", "2020-12-31"),
    ("2021 结构市", "2021-01-01", "2021-12-31"),
    ("2022 熊市", "2022-01-01", "2022-12-31"),
    ("2023-2024 震荡", "2023-01-01", "2024-12-31"),
    ("2025-2026 牛市/调整", "2025-01-01", "2026-12-31"),
]


def query(sql: str, timeout: int = 300) -> pd.DataFrame:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    cols = [c["name"] for c in payload["columns"]]
    return pd.DataFrame(payload["dataset"], columns=cols)


def build_panel(pool_date: str, pool_size: int, start: str) -> pd.DataFrame:
    pool = query(
        f"""select code from ohlcv_1d where ts='{pool_date}' and market='MLCN'
            order by amount desc limit {pool_size}"""
    )
    codes = [str(c) for c in pool["code"]]
    logger.info("点位池 %s 前 %s 只", pool_date, len(codes))
    frames = []
    for i in range(0, len(codes), 200):
        batch = codes[i : i + 200]
        inlist = ",".join(f"'{c}'" for c in batch)
        frames.append(
            query(
                f"""select code, ts, open, high, low, close, volume from ohlcv_1d
                    where ts >= '{start}' and market='MLCN' and code in ({inlist})"""
            )
        )
        logger.info("  pulled %s", i + len(batch))
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column])
    df = df.sort_values(["code", "ts"]).reset_index(drop=True)

    g = df.groupby("code", group_keys=False)
    df["ret"] = g["close"].pct_change()
    df["mom20"] = g["close"].apply(lambda s: s / s.shift(20) - 1)
    df["volma20"] = g["volume"].apply(lambda s: s.rolling(20).mean().shift(1))
    df["vr"] = df["volume"] / df["volma20"]
    df["n_open"] = g["open"].shift(-1)
    for k in (5, 20):
        df[f"r{k}"] = g["close"].shift(-k) / df["n_open"] - 1
    return df


class Panel:
    """预先把日期×代码的价量表展开成矩阵，避免每次回测重算。"""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.dates = np.array(sorted(df["ts"].unique()))
        self.dpos = {d: i for i, d in enumerate(self.dates)}
        self.codes = sorted(df["code"].unique())
        self.cpos = {c: i for i, c in enumerate(self.codes)}
        self.close = np.full((len(self.dates), len(self.codes)), np.nan)
        self.open = np.full((len(self.dates), len(self.codes)), np.nan)
        for ts, o, c, code in df[["ts", "open", "close", "code"]].itertuples(index=False):
            i, j = self.dpos[ts], self.cpos[code]
            self.close[i, j] = c
            self.open[i, j] = o


def backtest(panel: "Panel", signals, *, max_pos=10, hold=20, cost=COST):
    dates, dpos, cpos = panel.dates, panel.dpos, panel.cpos
    close_m, open_m = panel.close, panel.open

    active = []
    for ts, grp in signals.sort_values("ts").groupby("ts"):
        i = dpos.get(ts)
        if i is None or i + 1 >= len(dates):
            continue
        for code in grp.head(max_pos)["code"]:
            j = cpos[code]
            px = open_m[i + 1, j]
            if np.isnan(px):
                continue
            active.append((j, i + 1, min(i + 1 + hold, len(dates) - 1), px))

    port = np.zeros(len(dates))
    for i in range(len(dates)):
        rets = []
        for j, entry, exit_, px in active:
            if i < entry or i >= exit_:
                continue
            cur = close_m[i, j]
            if cur != cur:
                continue
            if i == entry:
                rets.append(cur / px - 1 - cost)
            else:
                prev = close_m[i - 1, j]
                if prev == prev and prev > 0:
                    rets.append(cur / prev - 1)
        if rets:
            port[i] = float(np.mean(rets))

    curve = np.cumprod(1 + port)
    n = int((port != 0).sum())
    peak = np.maximum.accumulate(curve)
    vol = port[port != 0].std() if n else float("nan")
    return {
        "days": n,
        "total": float(curve[-1] - 1) if n else 0.0,
        "mdd": float(((curve - peak) / peak).min()) if n else 0.0,
        "sharpe": float(port[port != 0].mean() / vol * np.sqrt(242)) if n and vol else float("nan"),
        "win": float((port[port != 0] > 0).mean()) if n else float("nan"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--pool", type=int, default=800)
    parser.add_argument("--start", default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--hold", type=int, default=20)
    parser.add_argument("--thresholds", default="1,3,5,6,9")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start = args.start or args.pool_date
    thresholds = [int(x) for x in args.thresholds.split(",") if x.strip()]

    df = build_panel(args.pool_date, args.pool, start)
    panel = Panel(df)
    logger.info("面板 %s 行 / %s 只 / %s 个交易日", f"{len(df):,}", df["code"].nunique(), df["ts"].nunique())

    sig = df.dropna(subset=["n_open", "r20", "mom20", "vr"])
    sig = sig[(sig.mom20 <= -0.10) & (sig.ret >= 0.05) & (sig.vr >= 1.5)].copy()
    density = sig.groupby("ts").size().rename("density")
    sig = sig.merge(density, left_on="ts", right_index=True, how="left")
    logger.info("信号 %s 个 / 信号日 %s 天", len(sig), sig["ts"].nunique())

    print("\n" + "=" * 104)
    print(f"跨市场状态验证（池: {args.pool_date} 前 {args.pool} 只；T+1 开盘买；持有 {args.hold} 日；扣 {COST:.1%}）")
    print("=" * 104)
    head = f"{'时段':<22}{'阈值':>5}{'持仓天':>7}{'累计':>10}{'年化':>10}{'最大回撤':>10}{'夏普':>8}{'日胜率':>8}"
    print(head)
    print("-" * 104)

    for name, s0, s1 in SEGMENTS:
        seg = sig[(sig.ts >= pd.Timestamp(s0)) & (sig.ts <= pd.Timestamp(s1))]
        if seg.empty:
            print(f"{name:<22}  （无信号）")
            continue
        for thr in thresholds:
            sel = seg[seg.density >= thr]
            if sel.empty:
                continue
            r = backtest(panel, sel, max_pos=args.top, hold=args.hold)
            if r["days"] == 0:
                continue
            base = 1 + r["total"]
            ann = (base ** (242 / r["days"]) - 1) if base > 0 else float("nan")
            print(f"{name:<22}{thr:>5}{r['days']:>7}{r['total']:>+10.1%}{ann:>+10.1%}"
                  f"{r['mdd']:>10.1%}{r['sharpe']:>8.2f}{r['win']:>8.0%}")
        print("-" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
