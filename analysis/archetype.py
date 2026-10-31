"""Archetype ('同类事件') statistics: interpretable base rates for the two setups.

A = 超跌后首根放量大阳线 (000949 today)
B = 暴涨后破位、缩量阴跌 (600418 today)

For every historical day in the 1,400-name panel that matches the archetype we
record the forward 2/5/14d return and the max/min path, then aggregate with a
recency-decayed weighting and date-block bootstrap confidence intervals.
"""
from __future__ import annotations

import json
import os
import pickle

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

HORIZONS = {"2d": 2, "5d": 5, "14d": 14}
BUCKETS = {
    "2d": [(-9, -0.05), (-0.05, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.03), (0.03, 0.05), (0.05, 9)],
    "5d": [(-9, -0.08), (-0.08, -0.05), (-0.05, -0.02), (-0.02, 0.02), (0.02, 0.05), (0.05, 0.08), (0.08, 9)],
    "14d": [(-9, -0.12), (-0.12, -0.07), (-0.07, -0.03), (-0.03, 0.03), (0.03, 0.07), (0.07, 0.12), (0.12, 9)],
}


def load() -> pd.DataFrame:
    with open(os.path.join(DATA, "pooled_feat.pkl"), "rb") as f:
        feat, scaler = pickle.load(f)
    return feat


def wq(x: np.ndarray, w: np.ndarray, q: float) -> float:
    o = np.argsort(x)
    x, w = x[o], w[o]
    cw = np.cumsum(w) / w.sum()
    return float(np.interp(q, cw, x))


def summarize(df: pd.DataFrame, w: np.ndarray) -> dict:
    out = {}
    for name in HORIZONS:
        x = df[f"fwd_{name}"].to_numpy()
        ok = ~np.isnan(x)
        xx, ww = x[ok], w[ok]
        probs = []
        for i, (lo, hi) in enumerate(BUCKETS[name]):
            m = (xx > lo) & (xx <= hi)
            probs.append(float(ww[m].sum() / ww.sum()))
        fmax = df[f"fmax_{name}"].to_numpy()[ok]
        fmin = df[f"fmin_{name}"].to_numpy()[ok]
        out[name] = {
            "n": int(ok.sum()),
            "ess": float(ww.sum() ** 2 / (ww ** 2).sum()),
            "mean": float(np.average(xx, weights=ww)),
            "median": wq(xx, ww, 0.5),
            "q10": wq(xx, ww, 0.10), "q25": wq(xx, ww, 0.25),
            "q75": wq(xx, ww, 0.75), "q90": wq(xx, ww, 0.90),
            "p_up": float(np.average((xx > 0), weights=ww)),
            "p_gt3": float(np.average((xx > 0.03), weights=ww)),
            "p_gt5": float(np.average((xx > 0.05), weights=ww)),
            "p_gt8": float(np.average((xx > 0.08), weights=ww)),
            "p_lt-3": float(np.average((xx < -0.03), weights=ww)),
            "p_lt-5": float(np.average((xx < -0.05), weights=ww)),
            "p_lt-8": float(np.average((xx < -0.08), weights=ww)),
            "p_fmax_gt3": float(np.average((fmax > 0.03), weights=ww)),
            "p_fmin_lt-3": float(np.average((fmin < -0.03), weights=ww)),
            "p_fmin_lt-5": float(np.average((fmin < -0.05), weights=ww)),
            "buckets": probs,
            "p_up_ci": bootstrap_ci(df[ok], name, ww),
        }
    return out


def bootstrap_ci(df: pd.DataFrame, name: str, w: np.ndarray, reps: int = 400) -> list[float]:
    """Date-block bootstrap: resample trading dates (not rows)."""
    d = df[["ts", f"fwd_{name}"]].copy()
    d["w"] = w
    d = d.dropna()
    if d.empty:
        return [float("nan"), float("nan")]
    d["blk"] = d["ts"].dt.to_period("M").astype(str)
    blocks = [(g[f"fwd_{name}"].to_numpy(), g["w"].to_numpy()) for _, g in d.groupby("blk")]
    if len(blocks) < 6:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(11)
    stats = []
    n = len(blocks)
    for _ in range(reps):
        idx = rng.integers(0, n, n)
        x = np.concatenate([blocks[i][0] for i in idx])
        ww = np.concatenate([blocks[i][1] for i in idx])
        stats.append(np.average((x > 0), weights=ww))
    return [float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))]


ARCH = {
    "000949": dict(
        label="超跌后放量大阳（r1∈[2.5%,9%]，量比≥1.5，60日回撤≤-15%，仍处MA20下方，成交额1-8亿）",
        expr=lambda d: (d.r1.between(0.025, 0.09)) & (d.vr >= 1.5) & (d.dd60 <= -0.15)
                        & (d.px_ma20 <= 0.02) & (d.vol20 >= 0.40) & (d.amount.between(1.0e8, 8e8)),
    ),
    "600418": dict(
        label="暴涨破位后缩量阴跌（r1∈[-6%,0.5%]，低于MA20≥8%，60日回撤≤-18%，20日跌≥5%，成交额≥8亿）",
        expr=lambda d: (d.r1.between(-0.06, 0.005)) & (d.px_ma20 <= -0.08) & (d.dd60 <= -0.18)
                        & (d.r20 <= -0.05) & (d.vol20 >= 0.55) & (d.amount >= 8e8),
    ),
}


def main() -> None:
    feat = load()
    feat = feat[feat["ts"] >= "2020-07-01"]
    weak = (feat["mkt_px_ma20"] <= 0.0) & (feat["mkt_r5"] <= -0.01)
    result = {}
    for code, cfg in ARCH.items():
        mask = cfg["expr"](feat).fillna(False)
        sel = feat[mask].dropna(subset=["fwd_14d"]).copy()
        age = (feat["ts"].max() - sel["ts"]).dt.days.to_numpy()
        # recent 3y gets full weight, decay afterwards
        w = np.exp(-np.log(2) * np.maximum(age - 500, 0) / 420.0) * 1.0
        stats = summarize(sel, w)

        # conditional on a weak tape (index below MA20 and 5d down), like today
        sel_weak = sel[weak.loc[sel.index]].copy()
        age_w = (feat["ts"].max() - sel_weak["ts"]).dt.days.to_numpy()
        w_w = np.exp(-np.log(2) * np.maximum(age_w - 500, 0) / 420.0)
        stats_weak = summarize(sel_weak, w_w) if len(sel_weak) > 200 else None

        # current-year regime
        sel_26 = sel[sel["ts"] >= "2026-01-01"].copy()
        stats_26 = summarize(sel_26, np.ones(len(sel_26))) if len(sel_26) > 100 else None

        # stability: split by calendar year
        by_year = {}
        for y, g in sel.groupby(sel["ts"].dt.year):
            if len(g) < 60:
                continue
            by_year[int(y)] = {h: float((g[f"fwd_{h}"] > 0).mean()) for h in HORIZONS}

        result[code] = {"label": cfg["label"], "stats": stats, "stats_weak": stats_weak,
                        "stats_2026": stats_26, "n": int(len(sel)), "n_weak": int(len(sel_weak)),
                        "by_year": by_year,
                        "years": [int(sel["ts"].dt.year.min()), int(sel["ts"].dt.year.max())]}
        print(f"\n### {code} {cfg['label']}  N={len(sel)}  weak-tape N={len(sel_weak)}")
        for name in HORIZONS:
            s = stats[name]
            print(f" {name} ALL : n={s['n']} ess={s['ess']:.0f} p_up={s['p_up']:.3f} "
                  f"CI={s['p_up_ci'][0]:.2f}-{s['p_up_ci'][1]:.2f} med={s['median']:+.3f} "
                  f"q10={s['q10']:+.3f} q90={s['q90']:+.3f} mean={s['mean']:+.3f}")
            print("    buckets", [round(b, 3) for b in s["buckets"]])
            if stats_weak:
                s = stats_weak[name]
                print(f" {name} WEAK: n={s['n']} p_up={s['p_up']:.3f} med={s['median']:+.3f} "
                      f"q10={s['q10']:+.3f} q90={s['q90']:+.3f} mean={s['mean']:+.3f}")
                print("    buckets", [round(b, 3) for b in s["buckets"]])
            if stats_26:
                s = stats_26[name]
                print(f" {name} 2026: n={s['n']} p_up={s['p_up']:.3f} med={s['median']:+.3f} "
                      f"mean={s['mean']:+.3f}")
                print("    buckets", [round(b, 3) for b in s["buckets"]])
        print(" year p_up(5d):", {y: round(v["5d"], 2) for y, v in by_year.items()})

    with open(os.path.join(OUT, "archetype.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
