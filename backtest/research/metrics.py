"""因子研究指标（不撮合）。

约定：
- 输入都是 date × symbol 宽表；
- 因子只使用 t 及之前的数据（由因子库保证）；
- forward returns 使用 t+h，仅用于评价，不能回流到信号。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def forward_returns(
    close: pd.DataFrame, horizons: tuple[int, ...] = (1, 5, 20)
) -> dict[int, pd.DataFrame]:
    data = close.copy().sort_index()
    return {
        int(h): data.shift(-int(h)).div(data).sub(1.0)
        for h in horizons
        if int(h) > 0
    }


def ic(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    *,
    method: str = "spearman",
) -> pd.Series:
    common_dates = factor.index.intersection(forward.index)
    common_symbols = factor.columns.intersection(forward.columns)
    if len(common_dates) == 0 or len(common_symbols) < 2:
        return pd.Series(dtype="float64")
    f = factor.loc[common_dates, common_symbols]
    r = forward.loc[common_dates, common_symbols]
    values = {}
    for dt in common_dates:
        row_f = f.loc[dt]
        row_r = r.loc[dt]
        pair = pd.concat([row_f.rename("f"), row_r.rename("r")], axis=1).dropna()
        if len(pair) < 2 or pair["f"].nunique() < 2 or pair["r"].nunique() < 2:
            continue
        if method == "spearman":
            # 不引入 scipy：Spearman = 秩的 Pearson。
            left = pair["f"].rank(method="average")
            right = pair["r"].rank(method="average")
            corr = left.corr(right)
        elif method == "pearson":
            corr = pair["f"].corr(pair["r"])
        else:
            raise ValueError(f"不支持的相关系数方法: {method!r}")
        if pd.notna(corr):
            values[dt] = float(corr)
    return pd.Series(values, dtype="float64").sort_index()


def ic_summary(series: pd.Series) -> dict[str, float]:
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return {"count": 0.0}
    std = float(clean.std(ddof=1)) if len(clean) > 1 else 0.0
    mean = float(clean.mean())
    return {
        "count": float(len(clean)),
        "mean": mean,
        "std": std,
        "t_stat": mean / (std / np.sqrt(len(clean))) if std > 0 and len(clean) > 1 else 0.0,
        "win_rate": float((clean > 0).mean()),
        "positive_rate": float((clean > 0).mean()),
    }


def quantile_returns(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    *,
    q: int = 5,
) -> pd.DataFrame:
    if q < 2:
        raise ValueError("q 必须 >= 2")
    common_dates = factor.index.intersection(forward.index)
    common_symbols = factor.columns.intersection(forward.columns)
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for dt in common_dates:
        pair = pd.concat(
            [
                factor.loc[dt, common_symbols].rename("factor"),
                forward.loc[dt, common_symbols].rename("forward"),
            ],
            axis=1,
        ).dropna()
        if len(pair) < q or pair["factor"].nunique() < 2:
            continue
        group = pd.qcut(
            pair["factor"].rank(method="first"),
            q,
            labels=False,
        )
        means = pair.groupby(group)["forward"].mean()
        row = {f"q{int(index) + 1}": float(value) for index, value in means.items()}
        if q >= 2:
            row["long_short"] = row.get(f"q{q}", float("nan")) - row.get("q1", float("nan"))
        rows[pd.Timestamp(dt)] = row
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    frame.index.name = "date"
    return frame


def turnover(factor: pd.DataFrame, *, top_n: int = 30) -> pd.Series:
    if top_n <= 0:
        raise ValueError("top_n 必须 > 0")
    previous: set[str] = set()
    values: dict[pd.Timestamp, float] = {}
    for dt in factor.index:
        row = factor.loc[dt].dropna()
        top = set(row.nlargest(int(top_n)).index.astype(str))
        if not previous:
            values[pd.Timestamp(dt)] = 0.0
        else:
            values[pd.Timestamp(dt)] = len(top - previous) / max(1, len(top))
        previous = top
    return pd.Series(values, dtype="float64").sort_index()


def decay(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    method: str = "spearman",
) -> pd.DataFrame:
    forwards = forward_returns(close, horizons)
    rows = {}
    for horizon, forward in forwards.items():
        summary = ic_summary(ic(factor, forward, method=method))
        rows[horizon] = summary
    frame = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    frame.index.name = "horizon"
    return frame
