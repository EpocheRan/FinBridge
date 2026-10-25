"""证券代码解析 / 交易所板块判定的单元测试（含 92x 北交所回归）。"""

from __future__ import annotations

import pytest

from data_interface.common.security_code import (
    board_of,
    is_fund_type,
    exchange_of,
    is_a_share_code,
    is_etf_code,
    is_lof_code,
    normalize_code,
    ticker_type,
    to_baostock_code,
    to_ts_code,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "600519"),
        ("600519.SH", "600519"),
        ("sh.600519", "600519"),
        ("SH600519", "600519"),
        ("000001.sz", "000001"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_code(raw, expected):
    assert normalize_code(raw) == expected


def test_920_is_beijing_not_shanghai():
    """旧实现把 9 开头一律当沪市，920xxx（北交所新代码段）会拼成 .SH。"""
    assert to_ts_code("920001") == "920001.BJ"
    assert to_baostock_code("920001") == "bj.920001"
    assert exchange_of("920001") == "BSE"
    assert board_of("920001") == "BSE"
    assert is_a_share_code("920001")


def test_classic_exchanges():
    assert to_ts_code("600519") == "600519.SH"
    assert to_ts_code("000001") == "000001.SZ"
    assert to_ts_code("300750") == "300750.SZ"
    assert to_ts_code("430047") == "430047.BJ"
    assert to_ts_code("830799") == "830799.BJ"


def test_boards():
    assert board_of("688981") == "STAR"
    assert board_of("300750") == "GEM"
    assert board_of("600519") == "MAIN"
    assert board_of("900901") == "B_SHARE"


def test_a_share_excludes_funds_and_b_shares():
    assert is_a_share_code("600519")
    assert not is_a_share_code("510300")
    assert not is_a_share_code("900901")
    assert not is_a_share_code("200002")


def test_ticker_types():
    # 场内 REITs / 封闭式基金：不能当股票（没有股票复权因子，也不走股票端点）
    assert ticker_type("508028") == "fund"
    assert ticker_type("180201") == "fund"
    assert ticker_type("184801") == "fund"
    assert is_fund_type("fund") and is_fund_type("etf") and not is_fund_type("stock")
    assert ticker_type("510300") == "etf"
    assert ticker_type("159915") == "etf"
    assert ticker_type("161725") == "lof"
    assert ticker_type("600519") == "stock"
    assert is_etf_code("588000")
    assert is_lof_code("501050")
