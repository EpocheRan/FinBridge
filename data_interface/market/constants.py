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

# 行情表 ``market`` 列（也是 DEDUP key 的一部分）
# 2026-10 起语义变更：market = 交易所（SSE/SZSE/BSE），品种由 ``type`` 区分。
# 2026-10-30 起：所有表的 ``market`` 都存交易所代码（SSE/SZSE/BSE），不再有 MLCN。
# ``MARKET_CN`` 仅为兼容旧调用点保留，写入端已不再使用。
MARKET_CN = "MLCN"

EXCHANGES = ("SSE", "SZSE", "BSE")
INSTRUMENT_TYPES = ("stock", "etf", "lof", "fund", "idx")


def market_scope_predicate(token: str | None, market_col: str = "market", type_col: str = "type") -> str:
    """把历史上的市场域标记翻译成新 schema 的 SQL 谓词。

    ``MLCN`` -> 非指数（股票 + 场内基金）；``IDX`` -> 指数；
    其它值按交易所/市场原样匹配。
    """
    if token in (None, "", "MLCN", "ALL"):
        return f"{type_col} <> 'idx'"
    if token == "IDX":
        return f"{type_col} = 'idx'"
    if token in EXCHANGES:
        return f"{market_col} = '{token}'"
    raise ValueError(f"未知的市场标记: {token}")


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
    "type",
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
    "type",
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
