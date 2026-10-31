"""Final blend, level probabilities, charts and markdown report."""
from __future__ import annotations

import json
import os
import pickle

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")

for f in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]:
    if os.path.exists(f):
        font_manager.fontManager.addfont(f)
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

HORIZONS = {"2d": 2, "5d": 5, "14d": 14}
LABELS = {
    "2d": ["≤-5%", "-5~-3%", "-3~-1%", "-1~+1%", "+1~+3%", "+3~+5%", ">+5%"],
    "5d": ["≤-8%", "-8~-5%", "-5~-2%", "-2~+2%", "+2~+5%", "+5~+8%", ">+8%"],
    "14d": ["≤-12%", "-12~-7%", "-7~-3%", "-3~+3%", "+3~+7%", "+7~+12%", ">+12%"],
}
NAMES = {"000949": "新乡化纤 000949", "600418": "江淮汽车 600418"}
LEVELS = {
    "000949": [("5.28 前低", 5.28), ("5.45 今日低", 5.45), ("5.56 昨收", 5.56), ("5.68 你的卖价", 5.68),
               ("5.73 现价", 5.73), ("5.94 今日高", 5.94), ("6.03 MA20", 6.03), ("6.12 涨停", 6.12)],
    "600418": [("17.37 跌停", 17.37), ("18.67 前低", 18.67), ("18.86 今日低", 18.86),
               ("19.01 现价", 19.01), ("19.34 你的成本", 19.335), ("19.38 启动点", 19.38),
               ("20.19 10/28收", 20.19)],
}
WEIGHTS = {"all": 0.45, "y26": 0.25, "own": 0.30}


def load_panel() -> pd.DataFrame:
    with open(os.path.join(DATA, "pooled_feat.pkl"), "rb") as f:
        feat, _ = pickle.load(f)
    return feat


def level_probs(df: pd.DataFrame, w: np.ndarray, p0: float, levels) -> dict:
    out = {}
    for label, lv in levels:
        need = lv / p0 - 1
        row = {}
        for name in HORIZONS:
            fmax = df[f"fmax_{name}"].to_numpy()
            fmin = df[f"fmin_{name}"].to_numpy()
            fwd = df[f"fwd_{name}"].to_numpy()
            if lv >= p0:
                hit = float(np.average(fmax >= need, weights=w))
                close = float(np.average(fwd >= need, weights=w))
            else:
                hit = float(np.average(fmin <= need, weights=w))
                close = float(np.average(fwd <= need, weights=w))
            row[name] = {"hit": hit, "close": close}
        out[label] = row
    return out


def main() -> None:
    arch = json.load(open(os.path.join(OUT, "archetype.json"), encoding="utf-8"))
    knn = json.load(open(os.path.join(OUT, "result.json"), encoding="utf-8"))
    feat = load_panel()
    feat = feat[feat["ts"] >= "2020-07-01"]
    weak = (feat["mkt_px_ma20"] <= 0.0) & (feat["mkt_r5"] <= -0.01)

    from archetype import ARCH

    final = {}
    for code in ("000949", "600418"):
        cfg = ARCH[code]
        mask = cfg["expr"](feat).fillna(False)
        sel = feat[mask].dropna(subset=["fwd_14d"])
        sel_weak = sel[weak.loc[sel.index]]
        sel_26 = sel[sel["ts"] >= "2026-01-01"]
        w_all = np.ones(len(sel))
        w_weak = np.ones(len(sel_weak))
        w_26 = np.ones(len(sel_26))

        snap = knn[code]["snap"]
        p0 = snap["price"]
        levels = LEVELS[code]

        rows = {}
        for name in HORIZONS:
            b_all = np.array(arch[code]["stats"][name]["buckets"])
            b_26 = np.array(arch[code]["stats_2026"][name]["buckets"])
            b_own = np.array(knn[code]["horizons"][name]["own"])
            blend = WEIGHTS["all"] * b_all + WEIGHTS["y26"] * b_26 + WEIGHTS["own"] * b_own
            blend = blend / blend.sum()
            pu_all = arch[code]["stats"][name]["p_up"]
            pu_26 = arch[code]["stats_2026"][name]["p_up"]
            pu_own = knn[code]["horizons"][name]["summary_own"]["p_up"]
            rows[name] = {
                "all": b_all.tolist(), "y26": b_26.tolist(), "own": b_own.tolist(),
                "blend": blend.tolist(),
                "p_up_all": pu_all, "p_up_26": pu_26, "p_up_own": pu_own,
                "p_up": WEIGHTS["all"] * pu_all + WEIGHTS["y26"] * pu_26 + WEIGHTS["own"] * pu_own,
                "med": arch[code]["stats"][name]["median"],
                "q10": arch[code]["stats"][name]["q10"],
                "q90": arch[code]["stats"][name]["q90"],
                "med_26": arch[code]["stats_2026"][name]["median"],
            }

        lv = {
            "all": level_probs(sel, w_all, p0, levels),
            "y26": level_probs(sel_26, w_26, p0, levels),
        }
        lv_blend = {}
        for label, lv0 in levels:
            lv_blend[label] = {
                name: {
                    "hit": 0.5 * lv["all"][label][name]["hit"] + 0.5 * lv["y26"][label][name]["hit"],
                    "close": 0.5 * lv["all"][label][name]["close"] + 0.5 * lv["y26"][label][name]["close"],
                } for name in HORIZONS
            }

        final[code] = {"name": NAMES[code], "snap": snap, "rows": rows, "levels": lv_blend,
                       "n_all": int(len(sel)), "n_26": int(len(sel_26))}

        print(f"\n=== {NAMES[code]} @ {p0} (n_all={len(sel)}, n_2026={len(sel_26)}) ===")
        for name in HORIZONS:
            r = rows[name]
            print(f"{name} blend", [round(x, 3) for x in r["blend"]],
                  f"p_up={r['p_up']:.3f} (all {r['p_up_all']:.3f} / 26 {r['p_up_26']:.3f} / own {r['p_up_own']:.3f})",
                  f"med={r['med']:+.3f} q10={r['q10']:+.3f} q90={r['q90']:+.3f}")
        for label, _ in levels:
            print(f"  {label:>14s}: " + "  ".join(
                f"{name} hit={lv_blend[label][name]['hit']:.2f} close={lv_blend[label][name]['close']:.2f}"
                for name in HORIZONS))

    with open(os.path.join(OUT, "final.json"), "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    make_charts(final)


def make_charts(final: dict) -> None:
    bars = {}
    for code in final:
        b = pd.read_csv(os.path.join(DATA, f"bars_{code}.csv"), parse_dates=["ts"])
        b["ma5"] = b["close"].rolling(5).mean()
        b["ma10"] = b["close"].rolling(10).mean()
        b["ma20"] = b["close"].rolling(20).mean()
        b["ma60"] = b["close"].rolling(60).mean()
        # append today's intraday price so the chart ends at "now"
        snap = final[code]["snap"]
        today = pd.DataFrame([{"ts": pd.Timestamp("2026-10-30"), "close": snap["price"]}])
        b = pd.concat([b, today], ignore_index=True)
        bars[code] = b

    for code, info in final.items():
        b = bars[code].tail(90).reset_index(drop=True)
        snap = info["snap"]
        fig, (ax, ax2) = plt.subplots(1, 2, figsize=(15, 5.6), gridspec_kw={"width_ratios": [1.12, 1]})

        ax.plot(b["ts"], b["close"], color="#1f3b73", lw=1.6, label="收盘/现价", marker="o", ms=2.2)
        for ma, c in (("ma5", "#e07b39"), ("ma10", "#8e44ad"), ("ma20", "#2e8b57"), ("ma60", "#b03a2e")):
            ax.plot(b["ts"], b[ma], lw=1.0, alpha=0.85, label=ma.upper())
        if code == "000949":
            for y, t, c in ((5.68, "你的卖价 5.68", "#c0392b"), (6.03, "MA20 6.03（上方压力）", "#2e8b57")):
                ax.axhline(y, color=c, ls=":", lw=1.0)
                ax.annotate(t, (b["ts"].iloc[1], y), xytext=(0, 3), textcoords="offset points",
                            fontsize=8, color=c)
            ax.axhline(5.72, color="#7f8c8d", ls=":", lw=1.0)
            ax.annotate("10/20 高点 5.72", (b["ts"].iloc[-20], 5.72), xytext=(0, -11),
                        textcoords="offset points", fontsize=8, color="#7f8c8d")
        else:
            ax.axhline(19.335, color="#c0392b", ls=":", lw=1.0)
            ax.annotate("你的成本 19.34", (b["ts"].iloc[1], 19.335), xytext=(0, 3),
                        textcoords="offset points", fontsize=8, color="#c0392b")
            ax.axhline(21.86, color="#2e8b57", ls=":", lw=1.0)
            ax.annotate("MA20 21.86", (b["ts"].iloc[1], 21.86), xytext=(0, 3),
                        textcoords="offset points", fontsize=8, color="#2e8b57")
            ax.axhline(18.67, color="#7f8c8d", ls=":", lw=1.0)
            ax.annotate("前低 18.67", (b["ts"].iloc[1], 18.67), xytext=(0, -10),
                        textcoords="offset points", fontsize=8, color="#7f8c8d")
        ax.set_title(f"{info['name']}  近90个交易日（末点为今日盘中价）", fontsize=12)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8, ncol=3, loc="upper left")
        ax.tick_params(axis="x", rotation=25, labelsize=8)

        mat = np.array([info["rows"][h]["blend"] for h in HORIZONS])
        norm = matplotlib.colors.TwoSlopeNorm(vcenter=1 / 7, vmin=0.06, vmax=0.24)
        im = ax2.imshow(mat, cmap="RdYlGn", norm=norm, aspect="auto")
        ax2.set_xticks(range(7), LABELS["2d"], fontsize=8.5, rotation=20)
        ax2.set_yticks(range(3), ["2个交易日", "5个交易日", "14个交易日"], fontsize=9.5)
        for i in range(3):
            for j in range(7):
                v = mat[i, j]
                ax2.text(j, i, f"{v*100:.0f}%", ha="center", va="center", fontsize=10, color="#222222")
            pu = info["rows"][list(HORIZONS)[i]]["p_up"]
            ax2.text(6.75, i, f"P(涨)={pu*100:.0f}%", ha="left", va="center", fontsize=9.5, color="#1f3b73")
        ax2.set_xlim(-0.5, 9.1)
        ax2.set_title("收益率分档概率矩阵（综合估计，绿=概率高于均匀分布）", fontsize=11)
        fig.colorbar(im, ax=ax2, fraction=0.03, pad=0.16)
        fig.tight_layout()
        path = os.path.join(OUT, f"matrix_{code}.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print("saved", path)


if __name__ == "__main__":
    main()
