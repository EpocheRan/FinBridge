"""技术指标（纯 pandas/numpy），从旧 ``indicator`` 模块迁入。

口径：
- ``method="EMA"``：对 True Range 做标准 EMA（旧实现默认）；
- ``method="WILDER"`` / ``"RMA"``：Wilder ATR（RMA，alpha=1/N）；
- ``method="SMA"`` / ``"WMA"``：简单/加权移动平均。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("Factors.Technical.ATR")


def _series(value) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.astype("float64")
    return pd.Series(value, dtype="float64")


def true_range(high, low, close) -> pd.Series:
    """True Range：max(high-low, |high-prev_close|, |low-prev_close|)。"""
    high_series = _series(high)
    low_series = _series(low)
    close_series = _series(close)
    prev_close = close_series.shift(1)
    return pd.concat(
        [
            high_series.sub(low_series),
            high_series.sub(prev_close).abs(),
            low_series.sub(prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)


def _atr_wilder(tr: pd.Series, n: int) -> pd.Series:
    """Wilder ATR（RMA）：首值用前 N 个 TR 的简单均值，之后递推。"""
    values = tr.to_numpy(dtype="float64", copy=False)
    result = np.full(len(values), np.nan, dtype="float64")
    period = max(1, int(n))
    if len(values) < period:
        return pd.Series(result, index=tr.index, name="atr")

    first = values[:period]
    first_valid = first[~np.isnan(first)]
    if len(first_valid) == 0:
        return pd.Series(result, index=tr.index, name="atr")

    result[period - 1] = float(first_valid.mean())
    for index in range(period, len(values)):
        current = values[index]
        previous = result[index - 1]
        if np.isnan(current):
            result[index] = previous
        elif np.isnan(previous):
            result[index] = current
        else:
            result[index] = (previous * (period - 1) + current) / period
    return pd.Series(result, index=tr.index, name="atr")


def _atr_wma(tr: pd.Series, n: int) -> pd.Series:
    weights = np.arange(1, n + 1, dtype="float64")
    weight_sum = weights.sum()
    return tr.rolling(n, min_periods=n).apply(
        lambda window: float(np.dot(window, weights) / weight_sum),
        raw=True,
    )


def atr(
    high,
    low,
    close,
    n: int = 14,
    method: str = "EMA",
) -> pd.Series:
    """计算 ATR，返回与输入索引对齐的 Series。"""
    period = max(1, int(n))
    tr = true_range(high, low, close)
    name = str(method or "EMA").strip().upper()

    if name in ("WILDER", "RMA"):
        result = _atr_wilder(tr, period)
    elif name == "SMA":
        result = tr.rolling(period, min_periods=period).mean()
    elif name == "WMA":
        result = _atr_wma(tr, period)
    elif name in ("EMA", ""):
        result = tr.ewm(span=period, adjust=False, min_periods=period).mean()
    else:
        logger.warning("Unknown ATR method %s, defaulting to EMA", method)
        result = tr.ewm(span=period, adjust=False, min_periods=period).mean()

    result.name = "atr"
    return result


def atr_wilder(high, low, close, window: int = 14) -> pd.Series:
    """兼容 screener 因子库命名的 Wilder ATR 函数。"""
    return atr(high, low, close, n=window, method="WILDER")


class ATR:
    """A 股/单标的 ATR 指标，保留旧的 ``ATR(N=14)`` 调用方式。"""

    def __init__(self, N: int = 14, method: str = "EMA"):
        self.N = max(1, int(N))
        self.method = method

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        if not isinstance(data, pd.DataFrame):
            raise TypeError("ATR.calculate expects a pandas DataFrame")

        frame = data.copy()
        frame.columns = [str(column).lower() for column in frame.columns]
        missing = {"high", "low", "close"} - set(frame.columns)
        if missing:
            raise ValueError(f"ATR data missing columns: {sorted(missing)}")

        return atr(
            frame["high"],
            frame["low"],
            frame["close"],
            n=self.N,
            method=self.method,
        )
