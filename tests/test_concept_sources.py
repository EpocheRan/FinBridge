"""概念板块数据源离线测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.concept.models import Board
from data_interface.concept.sources.em_source import EmConceptSource
from data_interface.concept.sources.ths_scrape_source import ThsScrapeConceptSource, _parse_members
from data_interface.concept.sources.ths_source import ThsConceptSource
from data_interface.concept.sources.ths_fast import ThsFastBoardSource
from data_interface.concept.sources.tushare_classification import (
    TushareConceptClassificationSource,
)


class FakeAk:
    def __init__(self):
        self.index_kwargs = None

    def stock_board_concept_name_ths(self):
        return pd.DataFrame({"name": ["人工智能", "AI手机"], "code": ["308614", "309120"]})

    def stock_board_concept_index_ths(self, **kwargs):
        self.index_kwargs = kwargs
        return pd.DataFrame(
            {
                "日期": ["2026-10-26", "2026-10-27"],
                "开盘价": [1200.0, 1232.0],
                "最高价": [1245.0, 1241.0],
                "最低价": [1223.0, 1188.0],
                "收盘价": [1232.0, 1188.0],
                "成交量": [24504670000, 22443392000],
                "成交额": [4.029666e11, 3.476997e11],
            }
        )

    def stock_board_concept_cons_em(self, symbol):
        return pd.DataFrame({"代码": ["300291", "688039"], "名称": ["华录百纳", "虹软科技"]})


def test_ths_list_boards_normalizes_codes():
    source = ThsConceptSource(ak=FakeAk())
    frame = source.list_boards()
    assert list(frame.columns) == ["board_code", "name", "board_type", "source", "source_code"]
    assert frame["board_code"].tolist() == ["308614", "309120"]
    assert frame["source"].iloc[0] == "akshare.ths_index"
    assert frame["board_type"].iloc[0] == "concept"


def test_ths_daily_columns_and_pct_change():
    ak = FakeAk()
    source = ThsConceptSource(ak=ak)
    board = Board(board_code="308614", name="人工智能", source="akshare.ths_index")
    frame = source.fetch_board_daily(board, "2026-10-20", "2026-10-27")

    assert list(frame.columns) == [
        "ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"
    ]
    assert len(frame) == 2
    assert frame["volume"].dtype.kind == "i"
    assert frame["volume"].iloc[0] == 24504670000
    assert frame["pct_chg"].iloc[0] != frame["pct_chg"].iloc[0]  # NaN
    assert frame["pct_chg"].iloc[1] == pytest.approx(1188.0 / 1232.0 - 1)
    assert ak.index_kwargs["start_date"] == "20261020"
    assert ak.index_kwargs["end_date"] == "20261027"


def test_em_members_normalizes_codes_and_name():
    source = EmConceptSource(ak=FakeAk())
    board = Board(board_code="308614", name="人工智能", source="akshare.em_board")
    frame = source.fetch_members(board)
    assert frame["stock_code"].tolist() == ["300291", "688039"]
    assert frame["stock_name"].tolist() == ["华录百纳", "虹软科技"]
    assert frame.attrs["source"] == "akshare.em_board"


def test_parse_members_from_html():
    html = """
    <table>
      <tr><th>序号</th><th>代码</th><th>名称</th><th>现价</th></tr>
      <tr><td>1</td><td>300291</td><td>华录百纳</td><td>6.24</td></tr>
      <tr><td>2</td><td>688039</td><td>虹软科技</td><td>45.08</td></tr>
    </table>
    """
    frame = _parse_members(html)
    assert frame["stock_code"].tolist() == ["300291", "688039"]
    assert frame["stock_name"].tolist() == ["华录百纳", "虹软科技"]


def test_parse_members_without_table_returns_empty():
    frame = _parse_members("<html><body>no table</body></html>")
    assert frame.empty
    assert list(frame.columns) == ["stock_code", "stock_name"]


def test_ths_scrape_uses_injected_session():
    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.encoding = "gbk"

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self, pages):
            self.pages = pages
            self.calls = []

        def get(self, url, timeout):
            self.calls.append(url)
            index = 0 if "/page/" not in url else int(url.split("/page/")[1].split("/")[0]) - 1
            return FakeResponse(self.pages[min(index, len(self.pages) - 1)])

    page1 = """
    <table><tr><th>代码</th><th>名称</th></tr>
    <tr><td>300291</td><td>A</td></tr></table>
    """
    session = FakeSession([page1])
    source = ThsScrapeConceptSource(session=session, sleep_seconds=0, max_pages=2)
    board = Board(board_code="308614", name="人工智能", source="ths.scrape")
    frame = source.fetch_members(board)
    assert frame["stock_code"].tolist() == ["300291"]
    assert frame.attrs["partial"] is False
    assert len(session.calls) == 1


class FakeConceptPro:
    def concept(self):
        return pd.DataFrame(
            [
                {
                    "ts_code": "800001.TS",
                    "name": "传媒",
                    "code": "800001.TS",
                    "class1_code": "C001",
                    "class1_name": "科技",
                    "class2_code": "C001006",
                    "class2_name": "传媒",
                    "class3_code": "C001006002",
                    "class3_name": "影视",
                    "trade_date": "20261027",
                    "item_size": 64,
                }
            ]
        )


def test_tushare_classification_maps_columns():
    source = TushareConceptClassificationSource(pro=FakeConceptPro())
    frame = source.fetch_classification()
    assert frame["concept_code"].tolist() == ["800001"]
    assert frame["name"].tolist() == ["传媒"]
    assert frame["class3_name"].tolist() == ["影视"]
    assert int(frame["item_size"].iloc[0]) == 64
    assert frame["ts"].iloc[0] == pd.Timestamp("2026-10-27")


class FakeFastResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.encoding = "gbk"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class FakeFastSession:
    def __init__(self, detail_html: str, year_text: str):
        self.detail_html = detail_html
        self.year_text = year_text
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "/gn/detail/code/" in url:
            return FakeFastResponse(self.detail_html)
        return FakeFastResponse(self.year_text)


def test_fast_source_parses_year_file(monkeypatch):
    detail = '<html><input id="clid" value="885431"></html>'
    year = 'x({"data":"20221010,2387.5,2398.8,2319.4,2327.8,5399620700,83426920000;'
    year += '20221011,2336.6,2380.7,2327.5,2370.6,5317610100,89255980000"})'
    session = FakeFastSession(detail, year)
    source = ThsFastBoardSource(session=session)
    monkeypatch.setattr(source, "_v", lambda refresh=False: "test-cookie")

    board = Board(board_code="300008", name="测试板块", source="akshare.ths_index")
    frame = source.fetch_board_daily(board, "2022-10-10", "2022-10-11")

    assert list(frame.columns) == [
        "ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"
    ]
    assert len(frame) == 2
    assert frame["volume"].iloc[0] == 5399620700
    assert frame["pct_chg"].iloc[1] == pytest.approx(2370.6 / 2327.8 - 1)
    assert any("885431" in url for url in session.calls)


def test_fast_source_404_year_returns_empty(monkeypatch):
    detail = '<html><input id="clid" value="885431"></html>'

    class NotFoundSession(FakeFastSession):
        def get(self, url, **kwargs):
            if "/gn/detail/code/" in url:
                return FakeFastResponse(self.detail_html)
            return FakeFastResponse("", status_code=404)

    source = ThsFastBoardSource(session=NotFoundSession(detail, ""))
    monkeypatch.setattr(source, "_v", lambda refresh=False: "test-cookie")
    board = Board(board_code="300008", name="测试板块", source="akshare.ths_index")
    frame = source.fetch_board_daily(board, "2022-10-10", "2022-10-11")
    assert frame.empty
