"""横截面算子：全部按日期逐行计算，禁止全样本统计。

约定
----
- 所有函数输入输出都是 ``date × symbol`` 宽表，**逐日**做统计，绝不跨日。
- 缺失值一律保持 ``NaN``，**不做 0/中位数填充**（0 在 z 分数里等于"中位数"，
  会凭空制造信号）。合成时按"可用分量权重再归一"处理缺失。
- 方向统一走 :func:`signed_rank`：先转百分位秩，再对负向因子取 ``1 - rank``，
  这样所有分量都落在 ``[0, 1]`` 且方向一致，量纲差异不会让某个因子主导。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 截面回归最少样本数（低于此值退化为"减均值"）
DEFAULT_MIN_OBS = 10


def rank_pct(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.rank(axis=1, pct=True)


def zscore(panel: pd.DataFrame) -> pd.DataFrame:
    mean = panel.mean(axis=1)
    std = panel.std(axis=1)
    return panel.sub(mean, axis=0).div(std.replace(0, np.nan), axis=0)


def winsorize(panel: pd.DataFrame, q: float = 0.01) -> pd.DataFrame:
    """按日缩尾；``q`` 为单侧比例。样本太少时原样返回。"""
    if not 0 < q < 0.5:
        raise ValueError("q must be in (0, 0.5)")
    if panel.shape[1] < 2:
        return panel.copy()
    lower = panel.quantile(q, axis=1)
    upper = panel.quantile(1.0 - q, axis=1)
    return panel.clip(lower=lower, upper=upper, axis=0)


def top_n(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    if n <= 0:
        raise ValueError("n must be positive")
    return panel.rank(axis=1, ascending=False, method="first") <= n


def signed_rank(panel: pd.DataFrame, direction: int = 1) -> pd.DataFrame:
    """百分位秩 + 方向统一。

    ``direction >= 0``：值越大越好（rank 0→1）；
    ``direction < 0``：值越小越好（``1 - rank``，仍是 0→1，且最大对应最优）。
    """
    ranked = rank_pct(panel)
    return ranked if direction >= 0 else 1.0 - ranked


def composite(
    panels: list[pd.DataFrame],
    weights: list[float] | None = None,
    directions: list[int] | None = None,
    *,
    min_parts: int = 1,
) -> pd.DataFrame:
    """等权/加权 rank 合成。

    三个关键行为：

    1. **方向统一**：每个分量先 :func:`signed_rank`，再按权重累加；
    2. **缺失不填 0**：某只股票缺某个分量时，只在该股票**可用分量**上重新归一权重，
       而不是把缺失当 0 拉低分数；
    3. **最少分量**：可用分量数 < ``min_parts`` 时输出 ``NaN``（宁缺毋滥）。
    """
    if not panels:
        raise ValueError("composite 至少需要一个分量")
    n = len(panels)
    weights = list(weights) if weights is not None else [1.0] * n
    directions = list(directions) if directions is not None else [1] * n
    if len(weights) != n or len(directions) != n:
        raise ValueError("weights/directions 长度必须与 panels 一致")

    acc: pd.DataFrame | None = None
    weight_sum: pd.DataFrame | None = None
    count: pd.DataFrame | None = None

    for panel, weight, direction in zip(panels, weights, directions):
        magnitude = float(weight)
        sign = 1 if direction >= 0 else -1
        if magnitude < 0:  # 允许用负权重表达反向，等价于翻转方向
            magnitude = -magnitude
            sign = -sign
        part = signed_rank(panel, sign) * magnitude
        available = part.notna()
        acc = part.fillna(0.0) if acc is None else acc.add(part.fillna(0.0), fill_value=0.0)
        w_available = available.astype("float64") * magnitude
        weight_sum = w_available if weight_sum is None else weight_sum.add(w_available, fill_value=0.0)
        cnt = available.astype("float64")
        count = cnt if count is None else count.add(cnt, fill_value=0.0)

    assert acc is not None and weight_sum is not None and count is not None
    score = acc.div(weight_sum.replace(0.0, np.nan))
    if min_parts > 1:
        score = score.where(count >= min_parts)
    return score


def neutralize(
    panel: pd.DataFrame,
    industry: pd.DataFrame | None = None,
    size: pd.DataFrame | None = None,
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    return_residual: bool = True,
) -> pd.DataFrame:
    """按日对「行业哑变量 + log 市值」做线性回归，返回残差。

    - ``industry`` / ``size`` 都是 ``date × symbol`` 宽表；
    - 每个交易日独立回归，**不跨日**；
    - 当日有效样本 < ``min_obs``，或设计矩阵退化时，退化为「减当日截面均值」，
      保证方向不被破坏（而不是返回全 NaN）。
    """
    data = panel.astype("float64")
    result = pd.DataFrame(np.nan, index=data.index, columns=data.columns, dtype="float64")

    for dt in data.index:
        row = data.loc[dt]
        valid = row.notna()
        n_valid = int(valid.sum())
        if n_valid == 0:
            continue
        if n_valid < max(2, int(min_obs)):
            result.loc[dt, valid] = row[valid] - row[valid].mean()
            continue

        y = row[valid]
        columns: list[np.ndarray] = [np.ones(n_valid)]
        names: list[str] = ["const"]

        if industry is not None and dt in industry.index:
            labels = industry.loc[dt].reindex(y.index).astype("string")
            dummies = pd.get_dummies(labels, drop_first=True, dtype="float64")
            for name in dummies.columns:
                values = dummies[name].to_numpy(dtype="float64")
                if np.nanstd(values) > 0:
                    columns.append(np.nan_to_num(values))
                    names.append(f"ind_{name}")

        if size is not None and dt in size.index:
            s = pd.to_numeric(size.loc[dt].reindex(y.index), errors="coerce")
            s = s.fillna(s.median())
            logs = np.log(s.clip(lower=1e-9).to_numpy(dtype="float64"))
            if np.nanstd(logs) > 0:
                columns.append(np.nan_to_num(logs))
                names.append("log_size")

        if len(columns) == 1:
            result.loc[dt, valid] = y - y.mean()
            continue

        X = np.column_stack(columns)
        # 列满秩检查：样本数必须大于参数个数，否则退化为减均值
        if X.shape[0] <= X.shape[1] or not np.isfinite(X).all():
            result.loc[dt, valid] = y - y.mean()
            continue

        beta, *_ = np.linalg.lstsq(X, y.to_numpy(dtype="float64"), rcond=None)
        residual = y.to_numpy(dtype="float64") - X @ beta
        result.loc[dt, valid] = residual

    return result if return_residual else data - result


def nonlinear_size(size: pd.DataFrame) -> pd.DataFrame:
    """市值非线性因子：逐日对 `size^3 ~ size` 回归取残差（市值非线性）。

    `size` 通常是 `-log(总市值)`。残差代表市值效应中不能被线性规模解释的部分。
    """
    result = pd.DataFrame(np.nan, index=size.index, columns=size.columns, dtype="float64")
    for dt in size.index:
        row = size.loc[dt].dropna()
        if len(row) < 20:
            continue
        x = row.to_numpy(dtype="float64")
        y = x ** 3
        design = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(design, y, rcond=None)
        result.loc[dt, row.index] = y - design @ beta
    return result
