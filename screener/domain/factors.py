"""兼容层：纯数学已迁到 ``factors.ops`` / ``factors.technical``。

screener 内部与旧调用方继续 ``from screener.domain import factors``，
但实现只保留一份（factors 包）。
"""

from __future__ import annotations

from factors.ops import (
    atr_pct,
    atr_wilder,
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
from factors.ops.ts import _clean, _series

__all__ = [
    "atr_pct",
    "atr_wilder",
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
