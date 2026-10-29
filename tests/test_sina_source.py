"""新浪分钟线适配器离线测试：JSONP 解析、口径标准化、深度截断。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.market.sources.sina_source import (
    MAX_DATALEN,
    SinaMinuteSource,
    parse_jsonp,
)

PAYLOAD = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    'x([{"day":"2026-10-28 14:57:00","open":"38.04","high":"38.20","low":"37.92",'
    '"close":"38.02","volume":"42300","amount":"1610851.0"},'
    '{"day":"2026-10-28 15:00:00","open":"38.39","high":"38.39","low":"38.39",'
    '"close":"38.39","volume":"98200","amount":"3769898.0"}])'
)


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_parse_jsonp_handles_wrapper_and_null():
    rows = parse_jsonp(PAYLOAD)
    assert len(rows) == 2
    assert rows[-1]["close"] == "38.39"
    assert parse_jsonp("x(null);") == []
    assert parse_jsonp("not json") == []
    assert parse_jsonp("") == []


def test_symbol_prefix_mapping():
    source = SinaMinuteSource()
    assert source.fetch_raw.__self__ is source  # 绑定方法存在
    from data_interface.market.sources.sina_source import _sina_symbol

    assert _sina_symbol("603699") == "sh603699"
    assert _sina_symbol("000001") == "sz000001"
    assert _sina_symbol("300750") == "sz300750"
    assert _sina_symbol("920662") == "bj920662"


def test_available_frequencies():
    assert SinaMinuteSource.available("1min")
    assert SinaMinuteSource.available("5min")
    assert not SinaMinuteSource.available("d")
    assert not SinaMinuteSource.available("240min")


def test_standardize_keeps_share_units_and_fills_amount():
    frame = SinaMinuteSource._standardize(parse_jsonp(PAYLOAD), "603699")
    assert list(frame.columns) == ["open", "high", "low", "close", "volume", "amount", "code"]
    assert frame["volume"].iloc[-1] == 98200  # 股，不再乘 100
    assert frame["amount"].iloc[-1] == pytest.approx(3769898.0)
    assert frame.attrs["missing_amount"] is False

    rows = [{"day": "2026-10-28 09:35:00", "open": "34.67", "high": "34.95",
             "low": "34.50", "close": "34.74", "volume": "557100"}]
    frame2 = SinaMinuteSource._standardize(rows, "603699")
    assert frame2["amount"].iloc[0] == 0.0
    assert frame2.attrs["missing_amount"] is True


def test_fetch_ohlc_truncates_and_reports_coverage():
    source = SinaMinuteSource(session=FakeSession([FakeResponse(PAYLOAD)]))
    frame = source.fetch_ohlc("603699", "2026-09-01", "2026-10-28", "1min")
    assert len(frame) == 2
    assert frame.attrs["truncated"] is True
    assert str(frame.attrs["covered_from"])[:10] == "2026-10-28"


def test_fetch_ohlc_retries_then_succeeds():
    session = FakeSession([RuntimeError("boom"), FakeResponse(PAYLOAD)])
    source = SinaMinuteSource(session=session, retries=3)
    frame = source.fetch_ohlc("603699", "2026-10-28", "2026-10-28", "1min")
    assert len(frame) == 2
    assert len(session.calls) == 2


def test_fetch_ohlc_returns_empty_after_retries():
    session = FakeSession([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    source = SinaMinuteSource(session=session, retries=3)
    frame = source.fetch_ohlc("603699", "2026-10-28", "2026-10-28", "1min")
    assert frame.empty


def test_datalen_is_capped():
    source = SinaMinuteSource(max_datalen=5000)
    assert source.max_datalen == MAX_DATALEN
    with pytest.raises(ValueError):
        source.fetch_raw("603699", "d")


def test_missing_columns_and_bad_rows():
    rows = [{"day": None, "open": "1", "close": "1", "high": "1", "low": "1", "volume": "1"}]
    frame = SinaMinuteSource._standardize(rows, "603699")
    assert frame.empty
    assert SinaMinuteSource._standardize([], "603699").empty
