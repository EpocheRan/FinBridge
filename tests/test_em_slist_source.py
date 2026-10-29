"""东财 slist 板块归属适配器离线测试。"""

from __future__ import annotations

import pytest

from data_interface.concept.sources.em_slist_source import (
    EmSlistBoardSource,
    _secid,
    classify_board,
)

SLIST_PAYLOAD = {
    "rc": 0,
    "data": {
        "total": 4,
        "diff": [
            {"f12": "BK1396", "f13": 90, "f14": "金属制品", "f3": 4.41, "f62": 1334202640.0},
            {"f12": "BK0545", "f13": 90, "f14": "通用设备", "f3": 3.65, "f62": 1756990976.0},
            {"f12": "BK0159", "f13": 90, "f14": "江苏板块", "f3": 2.45, "f62": 4818105600.0},
            {"f12": "BK0577", "f13": 90, "f14": "核能核电", "f3": 1.29, "f62": 209037056.0},
        ],
    },
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_classify_board_types():
    assert classify_board("通用设备") == "industry"
    assert classify_board("金属制品") == "industry"
    assert classify_board("江苏板块") == "region"
    assert classify_board("核能核电") == "concept"
    assert classify_board("融资融券") == "index"
    assert classify_board("沪股通") == "index"
    assert classify_board("") == "concept"


def test_secid_prefixes():
    assert _secid("603699") == "1.603699"
    assert _secid("000001") == "0.000001"
    assert _secid("300750") == "0.300750"
    assert _secid("688981") == "1.688981"
    assert _secid("920662") == "0.920662"
    assert _secid("830799") == "0.830799"


def test_fetch_stock_boards_parses_rank_and_type():
    source = EmSlistBoardSource(session=FakeSession([FakeResponse(SLIST_PAYLOAD)]), request_interval=0)
    frame = source.fetch_stock_boards("603699")
    assert len(frame) == 4
    assert list(frame["rank"]) == [0, 1, 2, 3]
    assert frame.iloc[0]["board_id"] == "BK1396"
    assert frame.iloc[0]["board_type"] == "industry"
    assert frame.iloc[2]["board_type"] == "region"
    assert frame.attrs["source"] == "em.slist"


def test_fetch_stock_boards_retries_on_connection_error():
    session = FakeSession([ConnectionError("reset"), FakeResponse(SLIST_PAYLOAD)])
    source = EmSlistBoardSource(session=session, retries=3, request_interval=0)
    frame = source.fetch_stock_boards("603699")
    assert len(frame) == 4
    assert session.calls == 2


def test_fetch_stock_boards_empty_payload():
    source = EmSlistBoardSource(session=FakeSession([FakeResponse({"data": None})]), request_interval=0)
    assert source.fetch_stock_boards("603699").empty


def test_fetch_industry_peers():
    payload = {"data": {"diff": [{"f12": "002438", "f14": "江苏神通"},
                                 {"f12": "300257", "f14": "开山股份"},
                                 {"f12": "002438", "f14": "江苏神通"}]}}
    source = EmSlistBoardSource(session=FakeSession([FakeResponse(payload)]), request_interval=0)
    peers = source.fetch_industry_peers("603699")
    assert peers == ["002438", "300257"]


def test_invalid_code_raises():
    source = EmSlistBoardSource(session=FakeSession([]), request_interval=0)
    with pytest.raises(ValueError):
        source.fetch_stock_boards("")
