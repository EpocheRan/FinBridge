"""真实数据源/数据库的端到端测试（``RUN_LIVE_TESTS=1`` 才运行）。

与 ``tests/test_ohlc.py`` 等早期「打印型脚本」不同，这里的用例都有断言，
覆盖 Docker 里运行中的 QuestDB + 真实 Tushare/AkShare 网络。
"""

from __future__ import annotations

import os

import pytest

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.market.fetcher import MarketDataFetcher
from data_interface.market.loader import MarketDataLoader

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1",
    reason="需要真实数据源/QuestDB；设置 RUN_LIVE_TESTS=1 开启",
)


@pytest.fixture(scope="module")
def loader():
    return MarketDataLoader(config=get_config())


@pytest.fixture(scope="module")
def db():
    client = QuestDBClient(get_config())
    yield client
    client.close()


@pytest.mark.live
def test_questdb_has_adj_factor_for_all_stock_bars(db):
    """股票必须有复权因子；ETF/LOF 没有稳定因子源，允许 NULL。"""
    from data_interface.common.security_code import ticker_type

    null_codes = [
        str(row[0])
        for row in db.query('SELECT DISTINCT code FROM "ohlcv_1d" WHERE adj_factor IS NULL')
    ]
    stocks_without_factor = [code for code in null_codes if ticker_type(code) == "stock"]
    assert stocks_without_factor == [], f"股票缺复权因子: {stocks_without_factor[:10]}"
    total = db.query('SELECT count() FROM "ohlcv_1d"', fetch_all=False)[0]
    assert total > 0


@pytest.mark.live
def test_qfq_read_path_is_raw_plus_factor(loader, db):
    raw = loader.ohlc("600519", "2024-01-02", "2024-01-05", freq="d", adjust="none")["600519"]
    qfq = loader.ohlc("600519", "2024-01-02", "2024-01-05", freq="d", adjust="qfq")["600519"]
    assert not raw.empty and not qfq.empty
    assert qfq.attrs.get("adjust") == "qfq"
    assert qfq.attrs.get("adjust_fallback") is None
    assert "raw_close" in qfq.columns

    stored = db.query(
        'SELECT close, adj_factor FROM "ohlcv_1d" WHERE code=%s AND ts=\'2024-01-02\'',
        ("600519",),
        fetch_all=False,
    )
    # 库里的 close 必须是不复权价
    assert raw["close"].iloc[0] == pytest.approx(stored[0], rel=1e-6)
    # 前复权 = raw * adj / anchor，且 anchor 为库内最新因子
    anchor = db.query(
        'SELECT adj_factor FROM "ohlcv_1d" WHERE code=%s LATEST ON ts PARTITION BY code',
        ("600519",),
        fetch_all=False,
    )[0]
    expected = stored[0] * stored[1] / anchor
    assert qfq["close"].iloc[0] == pytest.approx(expected, rel=1e-4)


@pytest.mark.live
def test_tushare_market_source_units_match_daily_aggregate():
    """周线量额必须与日线合计一致（修过 ×100/×1000 的放大 bug）。"""
    source = MarketDataFetcher("tushare", config=get_config())
    weekly = source.get_ohlc("600519", "2024-01-22", "2024-01-26", "w")
    daily = source.get_ohlc("600519", "2024-01-22", "2024-01-26", "d")
    assert not weekly.empty and not daily.empty
    assert weekly["volume"].iloc[0] == pytest.approx(daily["volume"].sum(), rel=1e-6)
    assert weekly["amount"].iloc[0] == pytest.approx(daily["amount"].sum(), rel=1e-6)


@pytest.mark.live
def test_ticker_basic_info_returns_profile_columns(loader):
    frame = loader.ticker_basic_info("600519")
    assert frame is not None and not frame.empty
    row = frame.iloc[0]
    assert str(row["code"]) == "600519"
    assert row["name"]
    assert row.get("exchange") in ("SSE", "SZSE", "BSE")


@pytest.mark.live
def test_scheduled_state_cursor_is_monotonic(db):
    rows = db.query(
        'SELECT cursor FROM "scheduled_task_state" WHERE dataset=\'daily\' '
        "LATEST ON updated_at PARTITION BY task_name, dataset"
    )
    assert rows, "daily 任务状态应已存在"
    assert str(rows[0][0]) >= "2024-01-01"
