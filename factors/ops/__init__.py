"""因子算子包：纯数学 + 序列计算。

依赖方向：``factors.ops`` 只依赖 ``factors.library`` / ``factors.technical``，
不得 import screener / data_interface / backtest。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..errors import FactorError
from ..library import CUSTOM_FACTORS, FactorDef
from ..technical.atr import atr_wilder
from .ts import (
    _clean,
    _series,
    atr_pct,
    gap_pct,
    intraday_pct,
    market_cap,
    pct_change,
    prior_mean,
    range_pct,
    realized_volatility,
    rolling_beta,
    rolling_corr,
    rolling_high,
    rolling_low,
    rolling_mean,
    rolling_std,
    volume_stats,
)


def _adj_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """补出 hfq 价格列，供 ATR 等价格类因子使用。"""
    result = frame.copy()
    factor = result["adj_factor"].fillna(1.0) if "adj_factor" in result.columns else 1.0
    if "hfq_close" not in result.columns:
        result["hfq_close"] = result["close"] * factor
    result["hfq_high"] = result["high"] * factor
    result["hfq_low"] = result["low"] * factor
    result["hfq_open"] = result["open"] * factor
    return result


def compute_series(defn: FactorDef, frame: pd.DataFrame) -> pd.Series:
    """在单只股票、按日期升序的 frame 上计算一个序列因子。"""
    data = _adj_frame(frame)
    if defn.op == "mom":
        n = int(defn.args[0])
        return pct_change(data[defn.on], n)
    if defn.op == "ma":
        n = int(defn.args[0])
        return rolling_mean(data[defn.on], n)
    if defn.op == "atr":
        n = int(defn.args[0])
        return atr_wilder(
            data["hfq_high"],
            data["hfq_low"],
            data["hfq_close"],
            window=n,
        )
    if defn.op == "vol":
        n = int(defn.args[0])
        return realized_volatility(pct_change(data[defn.on]), n)
    if defn.op in CUSTOM_FACTORS:
        result = CUSTOM_FACTORS[defn.op](data, *defn.args)
        if not isinstance(result, pd.Series):
            result = pd.Series(result, index=data.index)
        return result.astype("float64")
    raise FactorError(f"未知因子算子: {defn.op!r} (factor={defn.name!r})")


def clean_series(series: pd.Series) -> pd.Series:
    return series.replace([np.inf, -np.inf], np.nan).astype("float64")


__all__ = [
    "atr_pct",
    "atr_wilder",
    "clean_series",
    "compute_series",
    "gap_pct",
    "intraday_pct",
    "market_cap",
    "pct_change",
    "prior_mean",
    "range_pct",
    "realized_volatility",
    "rolling_beta",
    "rolling_corr",
    "rolling_high",
    "rolling_low",
    "rolling_mean",
    "rolling_std",
    "volume_stats",
]
