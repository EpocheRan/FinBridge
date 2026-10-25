"""行情数据表名与频率常量。"""

from __future__ import annotations

OHLC_TABLE_BY_FREQUENCY = {
    "1min": "ohlcv_1m",
    "5min": "ohlcv_5m",
    "15min": "ohlcv_15m",
    "30min": "ohlcv_30m",
    "60min": "ohlcv_60m",
    "d": "ohlcv_1d",
    "w": "ohlcv_1w",
    "m": "ohlcv_1mo",
}

VALID_FREQUENCIES = tuple(OHLC_TABLE_BY_FREQUENCY.keys())


def ohlc_table_name(freq: str) -> str:
    if freq not in OHLC_TABLE_BY_FREQUENCY:
        raise ValueError(
            f"不支持的频率: {freq}，可选值: {', '.join(VALID_FREQUENCIES)}"
        )
    return OHLC_TABLE_BY_FREQUENCY[freq]
