"""Extra conditioning: large caps testing their 52-week low (600418 today)."""
from __future__ import annotations

import json
import os
import pickle

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
HORIZONS = {"2d": 2, "5d": 5, "14d": 14}


def main() -> None:
    with open(os.path.join(DATA, "pooled_feat.pkl"), "rb") as f:
        feat, _ = pickle.load(f)
    feat = feat.sort_values(["code", "ts"])
    g = feat.groupby("code", sort=False)["close"]
    feat["lo250"] = g.transform(lambda s: s.rolling(250, min_periods=120).min())
    feat["hi250"] = g.transform(lambda s: s.rolling(250, min_periods=120).max())
    feat["pos250"] = feat["close"] / feat["lo250"] - 1
    feat = feat[feat["ts"] >= "2021-01-01"]

    mask = ((feat["pos250"] <= 0.03) & (feat["px_ma20"] <= -0.08)
            & (feat["amount"] >= 8e8) & (feat["vol20"] >= 0.45))
    sel = feat[mask].dropna(subset=["fwd_14d"])
    print("N =", len(sel), "codes", sel["code"].nunique())

    out = {}
    for name in HORIZONS:
        for label, sub in (("all", sel), ("y26", sel[sel["ts"] >= "2026-01-01"])):
            x = sub[f"fwd_{name}"].to_numpy()
            fmin = sub[f"fmin_{name}"].to_numpy()
            fmax = sub[f"fmax_{name}"].to_numpy()
            out[f"{name}_{label}"] = {
                "n": int(len(x)),
                "p_up": float((x > 0).mean()),
                "med": float(np.median(x)),
                "q10": float(np.percentile(x, 10)),
                "q90": float(np.percentile(x, 90)),
                "mean": float(x.mean()),
                "p_lt-5": float((x < -0.05).mean()),
                "p_gt5": float((x > 0.05).mean()),
                "p_break_low": float((fmin < -0.005).mean()),
                "p_bounce3": float((fmax > 0.03).mean()),
            }
            print(name, label, {k: round(v, 3) for k, v in out[f"{name}_{label}"].items()})

    # yearly stability of the 5d hit rate
    by_year = {int(y): round(float((sub["fwd_5d"] > 0).mean()), 2)
               for y, sub in sel.groupby(sel["ts"].dt.year) if len(sub) > 40}
    print("5d p_up by year:", by_year)
    out["by_year"] = by_year
    with open(os.path.join(OUT, "low52.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
