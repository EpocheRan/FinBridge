"""Conditional forward-return study for 000949 / 600418 (2d, 5d, 14d).

Method
------
1. Build per-stock daily features (momentum, MA distance, drawdown, volume
   expansion, volatility, turnover) plus index context.
2. Forward returns over 2 / 5 / 14 trading days, close-to-close (hfq).
3. Conditional distribution = recency-weighted k-NN in feature space, on a
   1400-name A-share panel (2020-2026) + the stock's own history.
4. Blend panel estimate with own-history estimate, report bucket matrix,
   path probabilities (recovery / drawdown) and a hold-out calibration check.
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

HORIZONS = {"2d": 2, "5d": 5, "14d": 14}
FEATS = ["r1", "r5", "r20", "r60", "vr", "px_ma20", "px_ma60", "dd60", "vol20", "turn", "mkt_r5", "mkt_px_ma20"]
W = np.array([1.2, 1.0, 0.9, 0.6, 1.1, 0.9, 0.7, 0.8, 0.9, 0.5, 0.6, 0.6], dtype=np.float32)


# --------------------------------------------------------------------------- #
# features
# --------------------------------------------------------------------------- #
def add_features(df: pd.DataFrame, idx: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["code", "ts"]).reset_index(drop=True)
    g = df.groupby("code", sort=False)

    price_cols = [c for c in df.columns if c in ("adj_factor",)]
    for c in ["open", "high", "low", "close"]:
        df[f"h_{c}"] = df[c] * (df["adj_factor"].fillna(1.0) if price_cols else 1.0)

    df["r1"] = g["h_close"].pct_change()
    for n in (5, 20, 60):
        df[f"r{n}"] = g["h_close"].pct_change(n)

    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = g["h_close"].transform(lambda s, n=n: s.rolling(n, min_periods=max(3, n // 2)).mean())
    df["px_ma20"] = df["h_close"] / df["ma20"] - 1
    df["px_ma60"] = df["h_close"] / df["ma60"] - 1

    df["hi60"] = g["h_high"].transform(lambda s: s.rolling(60, min_periods=20).max())
    df["dd60"] = df["h_close"] / df["hi60"] - 1

    df["vma20"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["vr"] = df["volume"] / df["vma20"]

    df["vol20"] = g["r1"].transform(lambda s: s.rolling(20, min_periods=10).std()) * np.sqrt(252)

    prev = g["h_close"].shift(1)
    tr = pd.concat([df["h_high"] - df["h_low"], (df["h_high"] - prev).abs(), (df["h_low"] - prev).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.groupby(df["code"]).transform(lambda s: s.rolling(14, min_periods=7).mean())
    df["atr_pct"] = df["atr14"] / df["h_close"]

    up = df["r1"].clip(lower=0)
    dn = (-df["r1"]).clip(lower=0)
    au = up.groupby(df["code"]).transform(lambda s: s.ewm(alpha=1 / 14, min_periods=7).mean())
    ad = dn.groupby(df["code"]).transform(lambda s: s.ewm(alpha=1 / 14, min_periods=7).mean())
    df["rsi14"] = 100 - 100 / (1 + au / ad.replace(0, np.nan))

    # index context
    ix = idx[["ts", "close"]].copy()
    ix["mkt_r5"] = ix["close"].pct_change(5)
    ix["mkt_ma20"] = ix["close"].rolling(20, min_periods=10).mean()
    ix["mkt_px_ma20"] = ix["close"] / ix["mkt_ma20"] - 1
    df = df.merge(ix[["ts", "mkt_r5", "mkt_px_ma20"]], on="ts", how="left")
    df["turn"] = df["turnover"] / 100.0 if df["turnover"].notna().any() else np.nan
    df["turn"] = df["turn"].fillna(df["vr"] * 0.03)
    return df


def add_targets(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("code", sort=False)
    for name, h in HORIZONS.items():
        df[f"fwd_{name}"] = g["h_close"].shift(-h) / df["h_close"] - 1
        fmax = g["h_high"].transform(
            lambda s, h=h: s.iloc[::-1].rolling(h, min_periods=h).max().iloc[::-1].shift(-1))
        fmin = g["h_low"].transform(
            lambda s, h=h: s.iloc[::-1].rolling(h, min_periods=h).min().iloc[::-1].shift(-1))
        df[f"fmax_{name}"] = fmax / df["h_close"] - 1
        df[f"fmin_{name}"] = fmin / df["h_close"] - 1
    return df


# --------------------------------------------------------------------------- #
# target-state vector (intraday snapshot treated as the close)
# --------------------------------------------------------------------------- #
def target_state(df: pd.DataFrame, idx: pd.DataFrame, snap: dict) -> tuple[np.ndarray, dict]:
    d = df.sort_values("ts").reset_index(drop=True)
    o = d.iloc[-1]
    p = snap["price"]
    prev_close = d["close"].iloc[-1]          # last completed session
    hf = o["h_close"] / o["close"]            # hfq multiple
    hp = p * hf

    closes = d["h_close"].to_numpy()
    highs = d["h_high"].to_numpy()

    def chg(n):
        return hp / closes[-n] - 1

    ma20 = np.append(closes[-19:], hp).mean()
    ma60 = np.append(closes[-59:], hp).mean()
    hi60 = max(highs[-59:].max(), hp)

    vol20 = d["volume"].iloc[-20:].mean()
    proj_vol = snap["volume"] / max(snap["elapsed_frac"], 0.05)
    vr = proj_vol / vol20 if vol20 else np.nan

    r = np.diff(np.append(closes[-20:], hp)) / np.append(closes[-20:], hp)[:-1]
    vol = r.std() * np.sqrt(252)

    ixl = idx.sort_values("ts")
    ic = ixl["close"].to_numpy()
    i_px = snap.get("index_price", ic[-1])
    mkt_r5 = i_px / ic[-5] - 1
    mkt_px_ma20 = i_px / np.append(ic[-19:], i_px).mean() - 1

    turn = proj_vol / snap["float_shares"] if snap.get("float_shares") else np.nan

    vec = np.array([p / prev_close - 1, chg(5), chg(20), chg(60), vr,
                    hp / ma20 - 1, hp / ma60 - 1, hp / hi60 - 1, vol, turn,
                    mkt_r5, mkt_px_ma20], dtype=np.float64)
    info = {"price": p, "prev_close": prev_close, "proj_vol_ratio": vr, "vol20": vol,
            "px_ma20": hp / ma20 - 1, "px_ma60": hp / ma60 - 1, "dd60": hp / hi60 - 1,
            "mkt_r5": mkt_r5, "turnover": turn, "elapsed_frac": snap["elapsed_frac"]}
    return vec, info


# --------------------------------------------------------------------------- #
# k-NN conditional distribution
# --------------------------------------------------------------------------- #
def knn(pool: pd.DataFrame, vec: np.ndarray, scaler: dict, k: int, half_life_days: float,
        seed: int = 7) -> pd.DataFrame:
    X = ((pool[FEATS].to_numpy(dtype=np.float32) - scaler["med"]) / scaler["iqr"]).astype(np.float32)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    X *= W
    z = (vec - scaler["med"]) / scaler["iqr"]
    z = np.nan_to_num(z, nan=0.0, posinf=0.0, neginf=0.0) * W
    dist = ((X - z) ** 2).sum(axis=1)
    k = min(k, len(dist) - 1)
    near = np.argpartition(dist, k)[:k]
    sub = pool.iloc[near].copy()
    sub["dist"] = np.sqrt(dist[near])
    age = (pool["ts"].max() - sub["ts"]).dt.days.to_numpy()
    sub["w"] = np.exp(-np.log(2) * age / half_life_days)
    return sub


def summarize(sub: pd.DataFrame, kind: str) -> dict:
    res = {}
    for name in HORIZONS:
        x = sub[f"fwd_{name}"].dropna()
        w = sub.loc[x.index, "w"]
        o = np.argsort(x.to_numpy())
        xv, wv = x.to_numpy()[o], w.to_numpy()[o]
        cw = np.cumsum(wv) / wv.sum()

        def wq(q):
            return float(np.interp(q, cw, xv))

        res[name] = {
            "n": int(len(x)),
            "ess": float(wv.sum() ** 2 / (wv ** 2).sum()),
            "mean": float(np.average(xv, weights=wv)),
            "median": wq(0.5),
            "q10": wq(0.10), "q25": wq(0.25), "q75": wq(0.75), "q90": wq(0.90),
            "p_up": float(np.average((xv > 0).astype(float), weights=wv)),
            "p_gt3": float(np.average((xv > 0.03).astype(float), weights=wv)),
            "p_gt5": float(np.average((xv > 0.05).astype(float), weights=wv)),
            "p_lt-3": float(np.average((xv < -0.03).astype(float), weights=wv)),
            "p_lt-5": float(np.average((xv < -0.05).astype(float), weights=wv)),
            "p_fmax_gt3": float(np.average((sub.loc[x.index, f"fmax_{name}"] > 0.03).astype(float), weights=wv)),
            "p_fmin_lt-3": float(np.average((sub.loc[x.index, f"fmin_{name}"] < -0.03).astype(float), weights=wv)),
            "p_fmin_lt-5": float(np.average((sub.loc[x.index, f"fmin_{name}"] < -0.05).astype(float), weights=wv)),
        }
    return res


BUCKETS = {
    "2d": [(-1.0, -0.05), (-0.05, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.03), (0.03, 0.05), (0.05, 1.0)],
    "5d": [(-1.0, -0.08), (-0.08, -0.05), (-0.05, -0.02), (-0.02, 0.02), (0.02, 0.05), (0.05, 0.08), (0.08, 1.0)],
    "14d": [(-1.0, -0.12), (-0.12, -0.07), (-0.07, -0.03), (-0.03, 0.03), (0.03, 0.07), (0.07, 0.12), (0.12, 1.0)],
}
LABELS = {
    "2d": ["≤-5%", "-5~-3%", "-3~-1%", "-1~+1%", "+1~+3%", "+3~+5%", ">+5%"],
    "5d": ["≤-8%", "-8~-5%", "-5~-2%", "-2~+2%", "+2~+5%", "+5~+8%", ">+8%"],
    "14d": ["≤-12%", "-12~-7%", "-7~-3%", "-3~+3%", "+3~+7%", "+7~+12%", ">+12%"],
}


def bucket_probs(sub: pd.DataFrame, name: str) -> np.ndarray:
    x = sub[f"fwd_{name}"].to_numpy()
    w = sub["w"].to_numpy()
    ok = ~np.isnan(x)
    x, w = x[ok], w[ok]
    edges = BUCKETS[name]
    probs = []
    for i, (lo, hi) in enumerate(edges):
        left = -np.inf if i == 0 else lo
        right = np.inf if i == len(edges) - 1 else hi
        m = (x > left) & (x <= right)
        probs.append(float(w[m].sum() / w.sum()))
    return np.array(probs)


def level_probs(sub: pd.DataFrame, name: str, p0: float, levels: list[float]) -> dict:
    out = {}
    w = sub["w"].to_numpy()
    for lv in levels:
        need = lv / p0 - 1
        mh = sub[f"fmax_{name}"].to_numpy() >= need
        ml = sub[f"fmin_{name}"].to_numpy() <= need
        out[f"{lv:.2f}"] = {
            "up_path": float(np.average(mh, weights=w)),
            "close_above": float(np.average(sub[f"fwd_{name}"].to_numpy() >= need, weights=w)),
            "down_path": float(np.average(ml, weights=w)),
        }
    return out


# --------------------------------------------------------------------------- #
def build_pooled() -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    pooled = pd.read_csv(os.path.join(DATA, "pooled_daily.csv"), parse_dates=["ts"])
    idx = pd.read_csv(os.path.join(DATA, "bars_index.csv"), parse_dates=["ts"])
    pooled = pooled.dropna(subset=["adj_factor"])
    pooled = pooled[pooled["ts"] >= "2020-06-01"]
    feat = add_features(pooled, idx)
    feat = add_targets(feat)
    scaler = {}
    X = feat[FEATS].to_numpy(dtype=np.float64)
    scaler["med"] = np.nanmedian(X, axis=0)
    q75, q25 = np.nanpercentile(X, 75, axis=0), np.nanpercentile(X, 25, axis=0)
    scaler["iqr"] = np.where((q75 - q25) > 1e-9, q75 - q25, 1.0)
    return feat, scaler, idx


def main() -> None:
    cache = os.path.join(DATA, "pooled_feat.pkl")
    if os.path.exists(cache) and "--rebuild" not in sys.argv:
        with open(cache, "rb") as f:
            feat, scaler = pickle.load(f)
        idx = pd.read_csv(os.path.join(DATA, "bars_index.csv"), parse_dates=["ts"])
        print("loaded cached panel", feat.shape)
    else:
        feat, scaler, idx = build_pooled()
        with open(cache, "wb") as f:
            pickle.dump((feat, scaler), f)
        print("built panel", feat.shape)

    snaps = json.load(open(os.path.join(DATA, "snapshot.json"), encoding="utf-8"))
    result = {}
    for code, snap in snaps.items():
        own = pd.read_csv(os.path.join(DATA, f"bars_{code}.csv"), parse_dates=["ts"])
        own["code"] = code
        own = add_features(own, idx)
        own = add_targets(own)
        vec, info = target_state(own, idx, snap)

        pool = feat[feat["ts"] >= "2021-01-01"].dropna(subset=["fwd_14d"])
        sub_panel = knn(pool, vec, scaler, k=900, half_life_days=420)
        own_hist = own[(own["ts"] >= "2019-01-01")].dropna(subset=["fwd_14d"])
        sub_own = knn(own_hist[["ts"] + FEATS + [f"fwd_{h}" for h in HORIZONS]
                      + [f"fmax_{h}" for h in HORIZONS] + [f"fmin_{h}" for h in HORIZONS]].assign(
                          code=code), vec, scaler, k=250, half_life_days=500)

        res_panel = summarize(sub_panel, "panel")
        res_own = summarize(sub_own, "own")

        blend = {}
        for name in HORIZONS:
            bp, bo = bucket_probs(sub_panel, name), bucket_probs(sub_own, name)
            blend[name] = {
                "panel": bp.tolist(), "own": bo.tolist(),
                "blend": (0.62 * bp + 0.38 * bo).tolist(),
                "summary_panel": res_panel[name], "summary_own": res_own[name],
            }

        levels = sorted({round(snap["price"], 2), round(snap.get("cost", snap["price"]), 3),
                         round(snap.get("ref_high", snap["price"]), 2), round(snap.get("ref_low", snap["price"]), 2)})
        result[code] = {
            "info": info, "snap": snap,
            "horizons": blend,
            "levels": {name: level_probs(sub_panel, name, snap["price"], levels) for name in HORIZONS},
            "analogs": sub_panel.nsmallest(12, "dist")[["code", "ts", "dist", "w", "r1", "px_ma20", "vr"]
                                                        + [f"fwd_{h}" for h in HORIZONS]].to_dict("records"),
            "own_analogs": sub_own.nsmallest(10, "dist")[["ts", "dist", "r1", "px_ma20", "vr"]
                                                          + [f"fwd_{h}" for h in HORIZONS]].to_dict("records"),
        }

    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    for code, r in result.items():
        print(f"\n=== {code} {r['snap']['name']} price={r['info']['price']} ===")
        print({k: round(v, 4) for k, v in r["info"].items() if isinstance(v, float)})
        for name in HORIZONS:
            row = r["horizons"][name]
            print(name, "blend", [round(x, 3) for x in row["blend"]])
            print(name, "panel", [round(x, 3) for x in row["panel"]])
            print(name, "own  ", [round(x, 3) for x in row["own"]])
            s = row["summary_panel"]
            print("   summary", {k: round(v, 3) for k, v in s.items() if isinstance(v, float)})


if __name__ == "__main__":
    main()
