"""时序因子纯函数（从 screener.domain.factors 迁入）。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _series(value) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.astype("float64")
    return pd.Series(value, dtype="float64")


def _clean(series: pd.Series) -> pd.Series:
    return series.replace([np.inf, -np.inf], np.nan)


def pct_change(close, periods: int = 1) -> pd.Series:
    series = _series(close)
    return _clean(series.div(series.shift(periods)).sub(1.0))


def gap_pct(open_price, prev_close) -> pd.Series:
    open_series = _series(open_price)
    prev = _series(prev_close)
    return _clean(open_series.div(prev).sub(1.0))


def intraday_pct(close, open_price) -> pd.Series:
    return _clean(_series(close).div(_series(open_price)).sub(1.0))


def range_pct(high, low, prev_close) -> pd.Series:
    high_series = _series(high)
    low_series = _series(low)
    prev = _series(prev_close)
    return _clean(high_series.sub(low_series).div(prev))


def rolling_mean(series, window: int, min_periods: int | None = None) -> pd.Series:
    return _series(series).rolling(
        window, min_periods=min_periods or window
    ).mean()


def prior_mean(series, window: int, min_periods: int | None = None) -> pd.Series:
    """前 N 日均值，不包含当日。"""
    return rolling_mean(_series(series).shift(1), window, min_periods)


def rolling_std(series, window: int, min_periods: int | None = None) -> pd.Series:
    return _series(series).rolling(
        window, min_periods=min_periods or window
    ).std(ddof=1)


def atr_pct(atr, close) -> pd.Series:
    return _clean(_series(atr).div(_series(close)))


def realized_volatility(
    returns,
    window: int = 20,
    *,
    annualize: int = 252,
    min_periods: int | None = None,
) -> pd.Series:
    """年化波动率，返回小数（0.25 = 25%）。"""
    value = rolling_std(returns, window, min_periods)
    return _clean(value * np.sqrt(annualize))


def rolling_beta(
    stock_returns,
    benchmark_returns,
    window: int = 252,
    min_periods: int = 60,
) -> pd.Series:
    stock = _series(stock_returns)
    bench = _series(benchmark_returns)
    stock, bench = stock.align(bench, join="inner")
    if stock.empty:
        return pd.Series(dtype="float64")
    covariance = stock.rolling(window, min_periods=min_periods).cov(bench)
    variance = bench.rolling(window, min_periods=min_periods).var(ddof=1)
    return _clean(covariance.div(variance))


def rolling_corr(
    stock_returns,
    benchmark_returns,
    window: int = 252,
    min_periods: int = 60,
) -> pd.Series:
    stock = _series(stock_returns)
    bench = _series(benchmark_returns)
    stock, bench = stock.align(bench, join="inner")
    if stock.empty:
        return pd.Series(dtype="float64")
    return _clean(stock.rolling(window, min_periods=min_periods).corr(bench))


def rolling_high(series, window: int, min_periods: int | None = None) -> pd.Series:
    return _series(series).rolling(
        window, min_periods=min_periods or window
    ).max()


def rolling_low(series, window: int, min_periods: int | None = None) -> pd.Series:
    return _series(series).rolling(
        window, min_periods=min_periods or window
    ).min()


def volume_stats(volume, windows: tuple[int, ...] = (5, 20, 60)) -> dict[int, pd.Series]:
    return {window: prior_mean(volume, window) for window in windows}


def market_cap(close, shares) -> pd.Series:
    return _clean(_series(close).mul(_series(shares)))
