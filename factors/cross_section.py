"""横截面算子：全部按日期逐行计算，禁止全样本统计。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rank_pct(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.rank(axis=1, pct=True)


def zscore(panel: pd.DataFrame) -> pd.DataFrame:
    mean = panel.mean(axis=1)
    std = panel.std(axis=1)
    return panel.sub(mean, axis=0).div(std.replace(0, np.nan), axis=0)


def winsorize(panel: pd.DataFrame, q: float = 0.01) -> pd.DataFrame:
    if not 0 < q < 0.5:
        raise ValueError("q must be in (0, 0.5)")
    lower = panel.quantile(q, axis=1)
    upper = panel.quantile(1.0 - q, axis=1)
    return panel.clip(lower=lower, upper=upper, axis=0)


def top_n(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    if n <= 0:
        raise ValueError("n must be positive")
    return panel.rank(axis=1, ascending=False, method="first") <= n


def neutralize(
    panel: pd.DataFrame,
    industry: pd.DataFrame,
    size: pd.Series | None = None,
) -> pd.DataFrame:
    """按日对行业哑变量（可选市值）做线性回归，返回残差。

    industry: date × symbol 的行业标签；size: date × symbol 的 log 市值。
    这里只做最简实现，MVP 不调用。
    """
    result = panel.copy().astype("float64")
    for dt in panel.index:
        row = panel.loc[dt].dropna()
        if row.empty:
            continue
        labels = industry.loc[dt].reindex(row.index).astype("string")
        dummies = pd.get_dummies(labels, drop_first=True, dtype="float64")
        design = [pd.Series(1.0, index=row.index), dummies]
        if size is not None and dt in size.index:
            design.append(pd.Series(np.log(size.loc[dt].reindex(row.index).clip(lower=1e-9))))
        X = pd.concat(design, axis=1).astype("float64")
        X = X.loc[:, X.std() > 0]
        beta, *_ = np.linalg.lstsq(X.to_numpy(), row.to_numpy(), rcond=None)
        residual = row.to_numpy() - X.to_numpy() @ beta
        result.loc[dt, row.index] = residual
    return result
