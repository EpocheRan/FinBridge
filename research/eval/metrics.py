"""统一指标：IC / 多空价差 / 换手 / 多头夏普 / 覆盖率。

口径固定，不接受逐因子覆盖：

- 前向收益 ``close[t+h] / open[t+1] - 1``（T+1 开盘进场，不用 close-to-close）；
- IC = 逐日横截面 Spearman，成分内计算，单日不足 ``min_names`` 不计；
- 多空价差 = 最高分位等权收益 − 最低分位等权收益，按 ``horizon`` 日一期；
- 净价差 = 毛价差 − 2 × 单边成本；
- 多头夏普 = 最高分位等权组合的期收益年化后 / 标准差；
- 覆盖率 = 有效 IC 天数 / 评估期交易日数，低于阈值判「证据不足」。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Membership, PanelData
from .spec import EvalSpec


def _pivot(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return frame.pivot_table(index="date", columns="symbol", values=column, aggfunc="last")


def forward_returns(panel: PanelData, spec: EvalSpec) -> pd.DataFrame:
    frame = panel.frame
    close = _pivot(frame, "hfq_close").sort_index()
    open_ = _pivot(frame, "hfq_open").reindex(index=close.index, columns=close.columns)
    return close.shift(-spec.horizon) / open_.shift(-1) - 1


def _mask_membership(values: pd.DataFrame, membership: Membership,
                     dates: pd.Index | None = None) -> pd.DataFrame:
    """按 PIT 成分遮蔽非成分股（向量化，避免逐行 .loc 赋值）。"""
    if not membership.enabled:
        return values
    index = values.index if dates is None else pd.Index(dates)
    columns = values.columns
    position = {symbol: i for i, symbol in enumerate(columns)}
    mask = np.zeros((len(index), len(columns)), dtype=bool)
    for row, dt in enumerate(index):
        allowed = membership.at(dt)
        if allowed is None:
            mask[row, :] = True
        elif allowed:
            positions = [position[symbol] for symbol in allowed if symbol in position]
            if positions:
                mask[row, positions] = True
    return values.reindex(index=index).where(
        pd.DataFrame(mask, index=index, columns=columns)
    )


def rank_ic(signal: pd.DataFrame, forward: pd.DataFrame, min_names: int) -> pd.Series:
    dates = signal.index.intersection(forward.index)
    sig = signal.reindex(dates)
    fwd = forward.reindex(dates)
    valid = sig.notna() & fwd.notna()
    counts = valid.sum(axis=1)
    a = sig.where(valid).rank(axis=1)
    b = fwd.where(valid).rank(axis=1)
    ic = a.corrwith(b, axis=1)
    return ic.where(counts >= int(min_names))


def _quantile_sets(signal: pd.Series, quantile: float, min_names: int) -> tuple[list[str], list[str]]:
    values = signal.dropna()
    if len(values) < int(min_names):
        return [], []
    n = max(1, int(len(values) * float(quantile)))
    ordered = values.sort_values(ascending=False)
    return list(ordered.index[:n]), list(ordered.index[-n:])


def _independent_t(ic: pd.Series, step: int) -> tuple[float, int]:
    series = ic.dropna()
    if len(series) <= step:
        return float("nan"), 0
    sample = series.iloc[::step]
    n = len(sample)
    if n < 3 or float(sample.std()) == 0:
        return float("nan"), n
    return float(sample.mean() / sample.std() * np.sqrt(n)), n


__all__ = ["forward_returns", "rank_ic"]
