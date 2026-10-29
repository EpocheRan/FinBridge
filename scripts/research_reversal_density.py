#!/usr/bin/env python
"""反转信号 × 信号密度：walk-forward 验证。

研究问题
--------
上一轮发现「超跌 + 放量反弹」类信号本身在事件研究里很漂亮，但**收益几乎全部来自
少数“市场级 capitulation”日**（同日几十上百只同时触发）。本脚本验证：

1. 用 2022-10 ~ 2024-12 定阈值（样本内）；
2. 用 2025-01 ~ 2026-10 检验（样本外）；
3. 阈值从 1 扫到 15，看「最优阈值」是否在样本外仍成立，还是纯粹的曲线拟合。

口径
----
- 信号日 T 收盘出信号，**T+1 开盘价买入**（不使用 T 日收盘价成交）；
- 持有 ``hold`` 个交易日，等权，最多 ``max_pos`` 只，双边成本 0.2%；
- 密度 = 当日触发信号的股票数（基于同一股票池，换池需重新标定）。

用法::

    python scripts/research_reversal_density.py
    python scripts/research_reversal_density.py --pool 800 --top 10 --hold 20
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

logger = logging.getLogger("Research.ReversalDensity")

COST = 0.002


# ---------------------------------------------------------------- 数据
def _questdb() -> str:
    cfg = get_config()
    host = resolve_questdb_host(cfg)
    return f"http://{host}:{cfg['questdb'].get('rest_port', 9000)}"


BASE = _questdb()


def query(sql: str, timeout: int = 300) -> pd.DataFrame:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    cols = [c["name"] for c in payload["columns"]]
    return pd.DataFrame(payload["dataset"], columns=cols)


def build_panel(pool_size: int, start: str = "2022-10-10") -> pd.DataFrame:
    pool = query(
        f"""select code from ohlcv_1d where ts='{start}' and market='MLCN'
            order by amount desc limit {pool_size}"""
    )
    codes = [str(c) for c in pool["code"]]
    logger.info("股票池：%s-10 成交额前 %s 只", start, len(codes))
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
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column])
    df = df.sort_values(["code", "ts"]).reset_index(drop=True)

    grouped = df.groupby("code", group_keys=False)
    df["ret"] = grouped["close"].pct_change()
    df["mom20"] = grouped["close"].apply(lambda s: s / s.shift(20) - 1)
    df["volma20"] = grouped["volume"].apply(lambda s: s.rolling(20).mean().shift(1))
    df["vr"] = df["volume"] / df["volma20"]
    df["clpos"] = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)
    df["n_open"] = grouped["open"].shift(-1)
    for k in (1, 5, 10, 20):
        df[f"c{k}"] = grouped["close"].shift(-k)
        df[f"r{k}"] = df[f"c{k}"] / df["n_open"] - 1
    return df


def add_regime(df: pd.DataFrame) -> pd.DataFrame:
    idx = query("select ts, close from ohlcv_1d where code='000852' and market='IDX' order by ts")
    idx["ts"] = pd.to_datetime(idx["ts"]).dt.tz_localize(None)
    idx["close"] = pd.to_numeric(idx["close"])
    idx["mom20"] = idx["close"] / idx["close"].shift(20) - 1
    idx["ma20"] = idx["close"].rolling(20).mean()
    idx["regime"] = np.where(
        (idx["mom20"] > 0) & (idx["close"] > idx["ma20"]),
        "bull",
        np.where((idx["mom20"] < -0.03) & (idx["close"] < idx["ma20"]), "bear", "neutral"),
    )
    return df.merge(idx[["ts", "regime"]], on="ts", how="left")


# ---------------------------------------------------------------- 回测
def backtest(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    max_pos: int = 10,
    hold: int = 20,
    cost: float = COST,
) -> dict:
    """T+1 开盘买入、持有 hold 日、等权的日历组合。"""
    dates = np.array(sorted(df["ts"].unique()))
    dpos = {d: i for i, d in enumerate(dates)}
    codes = sorted(df["code"].unique())
    cpos = {c: i for i, c in enumerate(codes)}
    close_m = np.full((len(dates), len(codes)), np.nan)
    open_m = np.full((len(dates), len(codes)), np.nan)
    for ts, o, c, code in df[["ts", "open", "close", "code"]].itertuples(index=False):
        i, j = dpos[ts], cpos[code]
        close_m[i, j] = c
        open_m[i, j] = o

    active: list[tuple[int, int, int, float]] = []
    for ts, grp in signals.sort_values("ts").groupby("ts"):
        i = dpos.get(ts)
        if i is None or i + 1 >= len(dates):
            continue
        for code in grp.head(max_pos)["code"]:
            j = cpos[code]
            price = open_m[i + 1, j]
            if np.isnan(price):
                continue
            active.append((j, i + 1, min(i + 1 + hold, len(dates) - 1), price))

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
    n_days = int((port != 0).sum())
    peak = np.maximum.accumulate(curve)
    mdd = float(((curve - peak) / peak).min()) if len(curve) else 0.0
    vol = port[port != 0].std() if n_days else float("nan")
    return {
        "days": n_days,
        "total": float(curve[-1] - 1) if len(curve) else 0.0,
        "cagr": float(curve[-1] ** (242 / n_days) - 1) if n_days else float("nan"),
        "mdd": mdd,
        "sharpe": float(port[port != 0].mean() / vol * np.sqrt(242)) if n_days and vol else float("nan"),
        "win": float((port[port != 0] > 0).mean()) if n_days else float("nan"),
        "curve": curve,
        "dates": dates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=int, default=800)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--hold", type=int, default=20)
    parser.add_argument("--is-end", default="2024-12-31")
    parser.add_argument("--oos-start", default="2025-01-01")
    parser.add_argument("--max-thr", type=int, default=15)
    parser.add_argument("--grid", action="store_true", help="对指定阈值做 top×hold 稳健性网格")
    parser.add_argument("--grid-thr", type=int, default=6)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    df = add_regime(build_panel(args.pool))
    sig = df.dropna(subset=["n_open", "r20", "mom20", "vr"])
    sig = sig[(sig.mom20 <= -0.10) & (sig.ret >= 0.05) & (sig.vr >= 1.5)].copy()
    density = sig.groupby("ts").size().rename("density")
    sig = sig.merge(density, left_on="ts", right_index=True, how="left")
    logger.info("信号 %s 个，信号日 %s 天", len(sig), sig["ts"].nunique())

    is_end = pd.Timestamp(args.is_end)
    oos_start = pd.Timestamp(args.oos_start)

    print("\n" + "=" * 96)
    print(f"walk-forward：样本内 ≤{args.is_end} ｜ 样本外 ≥{args.oos_start}（T+1开盘买，持有{args.hold}日，扣{COST:.1%}）")
    print("=" * 96)
    header = (f"{'阈值':>4}{'IS天数':>7}{'IS累计':>9}{'IS年化':>9}{'IS回撤':>8}{'IS夏普':>8}"
              f"{'OOS天数':>8}{'OOS累计':>9}{'OOS年化':>9}{'OOS回撤':>8}{'OOS夏普':>8}")
    print(header)
    print("-" * 96)

    rows = []
    for thr in range(1, args.max_thr + 1):
        sel = sig[sig.density >= thr]
        if sel.empty:
            continue
        res_is = backtest(df, sel[sel.ts <= is_end], max_pos=args.top, hold=args.hold)
        res_oos = backtest(df, sel[sel.ts >= oos_start], max_pos=args.top, hold=args.hold)
        rows.append((thr, res_is, res_oos))
        print(f"{thr:>4}{res_is['days']:>7}{res_is['total']:>+9.1%}{res_is['cagr']:>+9.1%}"
              f"{res_is['mdd']:>8.1%}{res_is['sharpe']:>8.2f}"
              f"{res_oos['days']:>8}{res_oos['total']:>+9.1%}{res_oos['cagr']:>+9.1%}"
              f"{res_oos['mdd']:>8.1%}{res_oos['sharpe']:>8.2f}")

    if args.grid:
        print("\n" + "=" * 96)
        print(f"参数稳健性网格（阈值 {args.grid_thr}，行为 top_n，列为持有天数）")
        print("=" * 96)
        print(f"{'top\\hold':>10}" + "".join(f"{h:>22}" for h in (10, 20, 30)))
        sel = sig[sig.density >= args.grid_thr]
        for top in (5, 10, 15):
            cells = []
            for hold in (10, 20, 30):
                r_is = backtest(df, sel[sel.ts <= is_end], max_pos=top, hold=hold)
                r_oos = backtest(df, sel[sel.ts >= oos_start], max_pos=top, hold=hold)
                cells.append(f"IS {r_is['sharpe']:5.2f}/OOS {r_oos['sharpe']:5.2f}")
            print(f"{top:>10}" + "".join(f"{c:>22}" for c in cells))
        print("（单元格为 夏普：样本内 / 样本外）")

    # 选阈值：只用样本内的夏普
    if rows:
        best = max(rows, key=lambda r: (r[1]["sharpe"] if r[1]["days"] > 60 else -9))
        thr = best[0]
        print("-" * 96)
        print(f"样本内最优阈值 = {thr}（IS 夏普 {best[1]['sharpe']:.2f}）→ "
              f"样本外：累计 {best[2]['total']:+.1%}，年化 {best[2]['cagr']:+.1%}，"
              f"回撤 {best[2]['mdd']:.1%}，夏普 {best[2]['sharpe']:.2f}")

        # 按年拆解（全样本，阈值=best）
        sel = sig[sig.density >= thr]
        full = backtest(df, sel, max_pos=args.top, hold=args.hold)
        print(f"\n全样本（阈值 {thr}）分年：")
        years = pd.Series(full["dates"]).dt.year.values
        rets = np.diff(np.concatenate([[1.0], full["curve"]])) if False else None
        port = np.zeros(len(full["curve"]))
        prev = np.concatenate([[1.0], full["curve"][:-1]])
        port = full["curve"] / prev - 1
        for y in sorted(set(years)):
            m = (years == y) & (port != 0)
            if m.sum() > 3:
                print(f"  {y}: 累计 {np.prod(1 + port[m]) - 1:+7.1%}  胜率 {(port[m] > 0).mean():.0%}  "
                      f"持仓天数 {int(m.sum())}")

        out = PROJECT_ROOT / "output" / "research"
        out.mkdir(parents=True, exist_ok=True)
        curve_df = pd.DataFrame({"date": full["dates"], "nav": full["curve"]})
        path = out / f"reversal_density_thr{thr}.csv"
        curve_df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n净值曲线已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
