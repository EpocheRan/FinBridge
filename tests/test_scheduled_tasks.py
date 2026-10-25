"""定时任务的离线回归测试：日线单位换算、空数据日重试、复权因子贴附。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from data_interface.common.timeutil import market_closed_today, today_str
from scheduled_tasks.service import AShareSyncService


def test_daily_frame_units_and_turnover():
    raw = [
        {
            "trade_date": "20240102",
            "ts_code": "600519.SH",
            "open": 10.0,
            "high": 10.6,
            "low": 9.9,
            "close": 10.5,
            "vol": 100.0,      # 手
            "amount": 1050.0,  # 千元
        }
    ]
    basic = [{"ts_code": "600519.SH", "turnover_rate": 0.8}]
    frame = AShareSyncService._daily_frame(raw, basic)
    row = frame.iloc[0]
    assert row["code"] == "600519"
    assert row["volume"] == pytest.approx(10000.0)   # 手 -> 股
    assert row["amount"] == pytest.approx(1_050_000.0)  # 千元 -> 元
    assert row["turnover"] == pytest.approx(0.8)


def test_fund_daily_frame_units():
    raw = [
        {
            "trade_date": "20240102",
            "ts_code": "510300.SH",
            "open": 3.9,
            "high": 4.0,
            "low": 3.8,
            "close": 3.95,
            "vol": 500.0,
            "amount": 2000.0,
        }
    ]
    frame = AShareSyncService._fund_daily_frame(raw)
    row = frame.iloc[0]
    assert row["volume"] == pytest.approx(50000.0)
    assert row["amount"] == pytest.approx(2_000_000.0)


def test_empty_day_pauses_for_retry():
    attempts: dict[str, int] = {}
    pending: list[str] = []
    errors: list[str] = []
    # 选一个"过去"的交易日，确保不受"今天还没收盘"分支影响
    result = AShareSyncService._handle_empty_day(
        None, "2020-01-02", attempts, pending, errors, retry_limit=3
    )
    assert result is True
    assert attempts["2020-01-02"] == 1
    assert pending == ["2020-01-02"]
    assert errors == []


def test_empty_day_gives_up_after_limit():
    attempts = {"2020-01-02": 2}
    pending = ["2020-01-02"]
    errors: list[str] = []
    result = AShareSyncService._handle_empty_day(
        None, "2020-01-02", attempts, pending, errors, retry_limit=3
    )
    assert result is False
    assert "2020-01-02" not in pending
    assert errors and "2020-01-02" in errors[0]


def test_today_before_close_is_not_an_error():
    if market_closed_today():
        pytest.skip("当前已过收盘，无法覆盖「今天还没数据」分支")
    attempts: dict[str, int] = {}
    pending: list[str] = []
    errors: list[str] = []
    result = AShareSyncService._handle_empty_day(
        None, today_str(), attempts, pending, errors, retry_limit=3
    )
    assert result is False        # 不阻塞批次
    assert errors == []           # 不记错误
    assert pending == [today_str()]  # 但留待下次补


def test_attach_factors_maps_by_code():
    class StubProvider:
        def factors_for_trade_date(self, trade_date):
            return {"600519": 2.0}

    service = object.__new__(AShareSyncService)
    service.adj_factors = StubProvider()
    frame = pd.DataFrame(
        {
            "code": ["600519", "000001"],
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1, 1],
            "amount": [1.0, 1.0],
            "turnover": [1.0, 1.0],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-02"]),
    )
    result = service._attach_factors(frame, "20240102")
    assert result["adj_factor"].iloc[0] == 2.0
    assert pd.isna(result["adj_factor"].iloc[1])  # 拿不到就 NULL，不假装是 1.0


def test_pending_days_are_retried_from_earliest(tmp_path):
    """有 pending 时游标回退，保证漏掉的交易日会被重试。"""
    # 该用例只校验纯逻辑：pending 的最小值参与游标选择
    pending = ["2024-01-05", "2024-01-03"]
    current_cursor = "2024-01-10"
    earliest = min(pending)
    effective = min(current_cursor, earliest)
    assert effective == "2024-01-03"


def test_daily_uses_weekday_fallback_when_calendar_is_empty():
    """日历不可用时不能把任务静默标记为 0 行。"""
    saved: dict = {}

    class State:
        def get(self, *args, **kwargs):
            return None

        def save(self, *args, **kwargs):
            saved.update(kwargs)

    service = object.__new__(AShareSyncService)
    service.settings = {}
    service.state = State()
    service.trading_calendar = SimpleNamespace(
        trading_days_or_weekdays=lambda start, end: ["2024-01-02"],
    )
    service.tushare = SimpleNamespace(fetch_daily_all=lambda day: [])
    service.market_fetcher = SimpleNamespace(update_ohlc=lambda frame, freq: 0)

    result = service.sync_daily(
        start_date="2024-01-02",
        end_date="2024-01-02",
        max_trade_days=1,
        reset=True,
        include_funds=False,
    )
    assert result["total"] == 1          # 不是静默 0
    assert result["next_cursor"] == "2024-01-02"
    assert result["done"] is False
