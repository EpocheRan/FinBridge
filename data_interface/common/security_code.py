"""证券代码规范化与交易所/板块判定（唯一实现）。

此前 ``600519`` / ``600519.SH`` / ``sh.600519`` 的解析在 market、financial
两处各写一份且规则不同（``sh.600519`` 会被解析成 ``sh``），这里统一收口。

代码段约定（A 股 + 场内基金）：

- 沪市：60x / 68x（科创板）/ 900（B 股）/ 5xx（ETF、LOF）
- 深市：00x / 30x（创业板）/ 200（B 股）/ 15x、16x、18x（ETF、LOF）
- 北交所：43x / 83x / 87x / 92x（920 为 2024 年新增段）
"""

from __future__ import annotations

import re

_BJ_PREFIXES = ("43", "83", "87", "92", "82", "88")
_SH_PREFIXES = ("60", "68", "90", "50", "51", "52", "56", "58")
_SZ_PREFIXES = ("00", "30", "20", "15", "16", "18", "12", "39")

_ETF_SH_PREFIXES = ("51", "56", "58", "52")
#: 场内 REITs / 封闭式基金（也是基金，但不是 ETF/LOF 端点能覆盖的品种）
_FUND_PREFIXES = ("508", "180", "184", "500", "150")
_ETF_SZ_PREFIXES = ("15", "16", "18")
_LOF_PREFIXES = ("16", "501", "502", "506")


def normalize_code(code: object) -> str:
    """提取 6 位证券代码：``600519`` / ``600519.SH`` / ``sh.600519`` / ``SH600519``。"""
    if code is None:
        return ""
    text = str(code).strip().upper()
    if not text:
        return ""
    if "." in text:
        for part in text.split("."):
            digits = re.sub(r"\D", "", part)
            if len(digits) >= 6:
                return digits[:6]
        text = text.replace(".", "")
    digits = re.sub(r"\D", "", text)
    return digits[-6:] if len(digits) >= 6 else digits


def exchange_of(code: str) -> str:
    """返回 ``SSE`` / ``SZSE`` / ``BSE``。"""
    code = normalize_code(code)
    if code.startswith(_BJ_PREFIXES):
        return "BSE"
    if code.startswith(_SH_PREFIXES):
        return "SSE"
    if code.startswith(_SZ_PREFIXES):
        return "SZSE"
    # 兜底：9 开头按沪市，其余深市
    return "SSE" if code.startswith("9") else "SZSE"


def board_of(code: str, market: str | None = None) -> str:
    """返回 ``MAIN`` / ``GEM`` / ``STAR`` / ``BSE`` / ``B_SHARE``。"""
    code = normalize_code(code)
    if code.startswith(("920", "92", "43", "83", "87", "82", "88")):
        return "BSE"
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("300", "301", "302")):
        return "GEM"
    if code.startswith(("900", "200")):
        return "B_SHARE"
    if market in ("创业板", "GEM"):
        return "GEM"
    if market in ("科创板", "STAR"):
        return "STAR"
    if market in ("北交所", "BSE"):
        return "BSE"
    return "MAIN"


def to_ts_code(code: object) -> str:
    """6 位代码 -> Tushare ``ts_code``（注意 92x 必须先于 9 -> SH 判断）。"""
    normalized = normalize_code(code)
    if not normalized:
        return ""
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange_of(normalized)]
    return f"{normalized}.{suffix}"


def to_baostock_code(code: object) -> str:
    """6 位代码 -> BaoStock 前缀形式（sh.600519 / sz.000001 / bj.920001）。"""
    normalized = normalize_code(code)
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}[exchange_of(normalized)]
    return f"{prefix}.{normalized}"


def is_bj_code(code: object) -> bool:
    return normalize_code(code).startswith(_BJ_PREFIXES)


def is_a_share_code(code: object) -> bool:
    """A 股（不含 B 股、不含基金）。"""
    normalized = normalize_code(code)
    if len(normalized) != 6:
        return False
    return normalized.startswith(
        ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003",
         "300", "301", "302", "43", "83", "87", "92")
    )


def is_etf_code(code: object) -> bool:
    normalized = normalize_code(code)
    return normalized.startswith(_ETF_SH_PREFIXES + _ETF_SZ_PREFIXES)


def is_lof_code(code: object) -> bool:
    normalized = normalize_code(code)
    return normalized.startswith(_LOF_PREFIXES)


def is_fund_type(value: str | None) -> bool:
    """``ticker_type`` 的返回值是否属于「场内基金」这一类。"""
    return value in ("etf", "lof", "fund")


def ticker_type(code: object) -> str:
    """``stock`` / ``etf`` / ``lof`` / ``fund``。

    ``fund`` 覆盖场内 REITs（508/180）与封闭式基金（500/184/150）：
    它们同样是基金、没有股票复权因子，不能走股票端点。
    """
    normalized = normalize_code(code)
    if not normalized:
        return "stock"
    if normalized.startswith(("16", "501", "502", "506")):
        return "lof"
    if normalized.startswith(("51", "56", "58", "159")):
        return "etf"
    if normalized.startswith(_FUND_PREFIXES):
        return "fund"
    return "stock"
