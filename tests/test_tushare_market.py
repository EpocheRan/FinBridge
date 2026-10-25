"""Tushare 作为可选行情源的离线测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.financial.sources.tushare_source import TushareSource
from data_interface.market.fetcher import MarketDataFetcher
from data_interface.market.sources.tushare_source import TushareMarketSource


class FakePriceApi:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[dict] = []

    def fetch_price_history(self, code, *, start_date=None, end_date=None, freq="d"):
        self.calls.append(
            {"code": code, "start": start_date, "end": end_date, "freq": freq}
        )
        return list(self.rows)


def _row(day: str = "20240102") -> dict:
    return {
        "ts_code": "600519.SH",
        "trade_date": day,
        "open": 10.0,
        "high": 10.6,
        "low": 9.9,
        "close": 10.5,
        "vol": 100.0,      # 手
        "amount": 1050.0,  # 千元
    }


def test_tushare_daily_returns_canonical_columns_and_units():
    api = FakePriceApi([_row()])
    source = TushareMarketSource({}, tushare=api)
    frame = source.fetch_ohlc("600519", "2024-01-01", "2024-01-03", "d")
    assert list(frame.columns) == [
        "code", "open", "high", "low", "close", "volume", "amount", "turnover"
    ]
    row = frame.iloc[0]
    assert row["code"] == "600519"
    assert row["close"] == pytest.approx(10.5)
    assert row["volume"] == 10_000       # 手 -> 股
    assert row["amount"] == 1_050_000    # 千元 -> 元
    assert pd.isna(row["turnover"])
    assert api.calls[0]["freq"] == "d"


def test_tushare_weekly_and_monthly_are_dispatched():
    api = FakePriceApi([_row("20240105")])
    source = TushareMarketSource({}, tushare=api)
    source.fetch_ohlc("600519", "2024-01-01", "2024-03-31", "w")
    source.fetch_ohlc("600519", "2024-01-01", "2024-03-31", "m")
    assert [call["freq"] for call in api.calls] == ["w", "m"]


def test_tushare_minute_is_not_supported():
    api = FakePriceApi([_row()])
    source = TushareMarketSource({}, tushare=api)
    assert source.fetch_ohlc("600519", "2024-01-02", "2024-01-02", "5min").empty
    assert api.calls == []


def test_fetcher_supports_tushare_price_frequencies():
    fetcher = MarketDataFetcher("tushare", config={"market": {}})
    assert fetcher.supports_frequency("d")
    assert fetcher.supports_frequency("w")
    assert fetcher.supports_frequency("m")
    assert not fetcher.supports_frequency("5min")


def test_tushare_source_requests_profile_status_fields():
    """stock_basic 必须显式要 list_status/delist_date，否则会写 NULL。"""
    captured: dict = {}

    class FakePro:
        @staticmethod
        def stock_basic(**kwargs):
            return None

    ts = TushareSource.__new__(TushareSource)
    ts._pro = FakePro()
    ts._call = lambda interface, func, **kwargs: captured.update(kwargs) or []
    ts.fetch_stock_basic(code="600519", list_status="L")
    fields = captured["fields"]
    assert "list_status" in fields
    assert "delist_date" in fields
    assert captured["ts_code"] == "600519.SH"


def test_tushare_weekly_is_not_rescaled():
    """周线 vol/amount 已是「股/元」，不能再乘 100/1000（曾导致放大 100 倍）。"""
    api = FakePriceApi(
        [
            {
                "ts_code": "600519.SH",
                "trade_date": "20240126",
                "open": 1629.0,
                "high": 1650.0,
                "low": 1600.0,
                "close": 1637.32,
                "vol": 17_315_769.0,          # 股
                "amount": 28_207_978_701.0,   # 元
            }
        ]
    )
    frame = TushareMarketSource({}, tushare=api).fetch_ohlc(
        "600519", "2024-01-22", "2024-01-26", "w"
    )
    assert frame["volume"].iloc[0] == 17_315_769
    assert frame["amount"].iloc[0] == pytest.approx(28_207_978_701.0)


def test_tushare_monthly_is_not_rescaled():
    api = FakePriceApi(
        [
            {
                "ts_code": "600519.SH",
                "trade_date": "20240131",
                "open": 1600.0,
                "high": 1700.0,
                "low": 1550.0,
                "close": 1637.32,
                "vol": 65_687_435.0,
                "amount": 107_236_681_733.0,
            }
        ]
    )
    frame = TushareMarketSource({}, tushare=api).fetch_ohlc(
        "600519", "2024-01-01", "2024-01-31", "m"
    )
    assert frame["volume"].iloc[0] == 65_687_435
    assert frame["amount"].iloc[0] == pytest.approx(107_236_681_733.0)


def test_tushare_daily_still_converts_lots_and_thousand_yuan():
    api = FakePriceApi([_row()])
    frame = TushareMarketSource({}, tushare=api).fetch_ohlc(
        "600519", "2024-01-02", "2024-01-02", "d"
    )
    assert frame["volume"].iloc[0] == 10_000
    assert frame["amount"].iloc[0] == pytest.approx(1_050_000.0)


def test_shared_tushare_source_keeps_one_limiter():
    """行情旁路/财务/定时任务必须共用同一个 TushareSource（一套限流与配额）。"""
    from data_interface.financial.sources.tushare_source import get_shared_tushare_source

    config = {"tushare": {"enabled": True, "token": "shared-token-for-test-only-1234567890"}}
    first = get_shared_tushare_source(config)
    second = get_shared_tushare_source(config)
    assert first is second
    assert first.limiter is second.limiter
    assert first.quota_guard.limit == second.quota_guard.limit

    captured: dict = {}

    class FakePro:
        @staticmethod
        def weekly(**kwargs):
            return None

    ts = TushareSource.__new__(TushareSource)
    ts._pro = FakePro()
    ts._call = lambda interface, func, **kwargs: captured.update(kwargs) or []
    ts.fetch_price_history("600519", start_date="2024-01-01", end_date="2024-03-31", freq="w")
    assert captured["start_date"] == "20240101"
    assert captured["end_date"] == "20240331"
    assert captured["ts_code"] == "600519.SH"
