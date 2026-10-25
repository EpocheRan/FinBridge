"""行情读写链路的离线集成测试（假 DB + 假数据源，不联网）。

覆盖：本地空 -> 缺失区间 -> 取数 -> 贴复权因子 -> 写库 -> 读取复权。
"""

from __future__ import annotations

import pandas as pd
import pytest

import data_interface.market.loader as loader_module
from data_interface.market.constants import OHLC_COLUMNS_DAILY
from data_interface.market.fetcher import MarketDataFetcher
from data_interface.market.loader import MarketDataLoader


class FakeDB:
    def __init__(self):
        self.inserted: list[tuple] = []
        self.sql: list[str] = []

    # 读取：本地没有数据
    def query_dataframe(self, sql, params=None):
        self.sql.append(sql)
        return pd.DataFrame()

    def query(self, sql, params=None, *, fetch_all=True):
        self.sql.append(sql)
        return None

    # 写入
    def execute_values(self, prefix, rows, page_size=1000):
        self.sql.append(prefix)
        self.inserted.extend(rows)
        return len(rows)

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return True


class StubSourceFetcher:
    """模拟 baostock/akshare 返回不复权日线。"""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_ohlc(self, code, start, end, freq="d", **kwargs):
        return self.frame.copy()

    def get_ticker_type(self, code):
        return "stock"


class StubCalendar:
    def trading_days_or_weekdays(self, start, end):
        return ["2024-01-02", "2024-01-03"]

    def latest_trading_day(self):
        return "2024-01-03"

    def is_trading_day(self, day):
        return True


class StubFactors:
    def __init__(self, mapping):
        self.mapping = mapping

    def daily_factors(self, code, start, end):
        return pd.Series(self.mapping, dtype="float64")


def _raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["600519", "600519"],
            "open": [10.0, 20.0],
            "high": [10.5, 20.5],
            "low": [9.5, 19.5],
            "close": [10.0, 20.0],
            "volume": [1000, 2000],
            "amount": [10000.0, 40000.0],
            "turnover": [1.0, 2.0],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )


def _build_loader(monkeypatch, factors):
    db = FakeDB()
    fetcher = MarketDataFetcher("questdb", config={"market": {}}, db=db)
    fetcher.latest_adj_factor = lambda code: 2.0  # type: ignore[assignment]

    loader = MarketDataLoader(
        config={"market": {}},
        fetcher=fetcher,
        trading_calendar=StubCalendar(),
        adj_factors=StubFactors(factors),
    )
    monkeypatch.setattr(
        loader_module,
        "MarketDataFetcher",
        lambda source, config=None, **kwargs: StubSourceFetcher(_raw_frame()),
    )
    return loader, db


def test_daily_pipeline_stores_raw_plus_factor_and_reads_adjusted(monkeypatch):
    loader, db = _build_loader(monkeypatch, {"2024-01-02": 1.0, "2024-01-03": 2.0})

    result = loader.ohlc("600519", "2024-01-02", "2024-01-03", freq="d", adjust="qfq")
    frame = result["600519"]

    # 两条「缺失」都被补齐并写库
    assert len(db.inserted) == 2
    assert any("adj_factor" in sql for sql in db.sql if "INSERT" in sql.upper())
    # 写入的是不复权价 + 因子
    assert db.inserted[1][OHLC_COLUMNS_DAILY.index("close")] == pytest.approx(20.0)
    assert db.inserted[1][OHLC_COLUMNS_DAILY.index("adj_factor")] == pytest.approx(2.0)

    # 读取端按 anchor=2.0 做前复权：2024-01-02 变成 5.0，最新一天保持 20.0
    assert frame["close"].iloc[0] == pytest.approx(5.0)
    assert frame["close"].iloc[1] == pytest.approx(20.0)
    assert frame["raw_close"].iloc[0] == pytest.approx(10.0)


def test_daily_pipeline_without_factors_marks_fallback(monkeypatch):
    loader, db = _build_loader(monkeypatch, {})
    result = loader.ohlc("600519", "2024-01-02", "2024-01-03", freq="d", adjust="qfq")
    frame = result["600519"]
    assert frame.attrs.get("adjust_fallback") == "missing_adj_factor"
    # 没有因子也不能把价格擅自改成别的
    assert frame["close"].iloc[0] == pytest.approx(10.0)
    # 因子列写 NULL 而不是 1.0（避免假装是不复权/复权一致的）
    assert pd.isna(db.inserted[0][OHLC_COLUMNS_DAILY.index("adj_factor")])


def test_update_ohlc_column_order_matches_ddl():
    db = FakeDB()
    fetcher = MarketDataFetcher("questdb", config={"market": {}}, db=db)
    frame = _raw_frame()
    frame["adj_factor"] = [1.0, 1.0]
    written = fetcher.update_ohlc(frame, "d")
    assert written == 2
    insert_sql = next(sql for sql in db.sql if sql.startswith("INSERT"))
    assert all(f'"{column}"' in insert_sql for column in OHLC_COLUMNS_DAILY)


def test_update_ohlc_skips_rows_without_prices():
    db = FakeDB()
    fetcher = MarketDataFetcher("questdb", config={"market": {}}, db=db)
    frame = _raw_frame()
    frame.loc[frame.index[0], "close"] = None
    assert fetcher.update_ohlc(frame, "d") == 1
    assert len(db.inserted) == 1


def test_ohlc_raises_when_db_read_fails(monkeypatch):
    """DB 故障不能被伪装成"本地无数据 -> 全量回源"。"""
    from data_interface.common.exceptions import SourceUnavailableError

    class BrokenDB(FakeDB):
        def query_dataframe(self, sql, params=None):
            raise RuntimeError("connection reset")

    fetcher = MarketDataFetcher("questdb", config={"market": {}}, db=BrokenDB())
    loader = MarketDataLoader(
        config={"market": {}},
        fetcher=fetcher,
        trading_calendar=StubCalendar(),
        adj_factors=StubFactors({}),
    )
    with pytest.raises(SourceUnavailableError):
        loader.ohlc("600519", "2024-01-02", "2024-01-03", freq="d")
