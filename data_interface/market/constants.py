"""行情数据表名、频率与存储口径常量。"""

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
INTRADAY_FREQUENCIES = tuple(freq for freq in VALID_FREQUENCIES if freq.endswith("min"))
DAILY_FREQUENCIES = ("d", "w", "m")

# 行情表 ``market`` 列（也是 DEDUP key 的一部分）；A 股统一取值
MARKET_CN = "MLCN"

#: 写入/建表共用的列顺序（唯一真源，schema 与 fetcher 都从这里取）
OHLC_COLUMNS_MINUTE = [
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "code",
    "market",
]
OHLC_COLUMNS_DAILY = [
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
    "adj_factor",
    "code",
    "market",
]

# 复权相关常量的稳定出口（实现见 data_interface.market.adjust）
DEFAULT_ADJUST = "qfq"


def ohlc_table_name(freq: str) -> str:
    if freq not in OHLC_TABLE_BY_FREQUENCY:
        raise ValueError(
            f"不支持的频率: {freq}，可选值: {', '.join(VALID_FREQUENCIES)}"
        )
    return OHLC_TABLE_BY_FREQUENCY[freq]


def normalize_frequency(freq: str | None) -> str:
    """规范化频率写法（60m/60min/1H/日线 等）。"""
    if freq is None:
        return "d"
    key = str(freq).strip().lower()
    aliases = {
        "d": "d",
        "day": "d",
        "daily": "d",
        "日": "d",
        "日线": "d",
        "w": "w",
        "week": "w",
        "weekly": "w",
        "周": "w",
        "周线": "w",
        "m": "m",
        "month": "m",
        "monthly": "m",
        "月": "m",
        "月线": "m",
    }
    if key in aliases:
        return aliases[key]
    normalized = key.replace("m", "min") if key.endswith("m") and not key.endswith("min") else key
    if normalized in OHLC_TABLE_BY_FREQUENCY:
        return normalized
    raise ValueError(
        f"不支持的频率: {freq}，可选值: {', '.join(VALID_FREQUENCIES)}"
    )
