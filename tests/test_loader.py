"""行情增量编排的离线测试：周期身份、缺失区间、实时拼接条件、复权锚点。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import data_interface.market.loader as loader_module
from data_interface.common.timeutil import market_closed_today, now_cn, today_str
from data_interface.market.loader import MarketDataLoader


class StubCalendar:
    def __init__(self, trading: bool = True):
        self.trading = trading

    def is_trading_day(self, day: str) -> bool:
        return self.trading

    def trading_days_or_weekdays(self, start, end):
        return []

    def latest_trading_day(self):
        return today_str()


def _loader(calendar=None) -> MarketDataLoader:
    loader = object.__new__(MarketDataLoader)
    loader.trading_calendar = calendar or StubCalendar()
    loader._adjust_anchor_cache = {}
    return loader


def test_weekly_period_identity_is_calendar_week():
    """跨年周不能被拆成两周：2024-12-30 ~ 2025-01-03 属于同一周。"""
    days = ["2024-12-30", "2024-12-31", "2025-01-02", "2025-01-03"]
    ends = MarketDataLoader._completed_period_ends(days, "w")
    assert ends == ["2025-01-03"]


def test_monthly_period_identity():
    days = ["2024-01-31", "2024-02-29", "2024-12-31"]
    ends = MarketDataLoader._completed_period_ends(days, "m")
    assert ends == days


def test_incomplete_current_period_is_not_written():
    if market_closed_today():
        pytest.skip("已收盘，当前周期视为完成")
    today = date.today().strftime("%Y-%m-%d")
    ends = MarketDataLoader._completed_period_ends([today], "w")
    assert ends == []


def test_check_missing_data_merges_consecutive_ranges():
    loader = _loader()
    trading_days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    local = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2024-01-05"]))
    missing = loader._check_missing_data(local, trading_days, trading_days, "d")
    assert missing == [("2024-01-02", "2024-01-04")]


def test_check_missing_data_for_minute_uses_expected_bar_count():
    loader = _loader()
    day = "2024-01-02"
    loader._calculate_expected_bars_per_day("5min")  # 48
    local = pd.DataFrame(
        {"close": [1.0] * 3}, index=pd.date_range("2024-01-02 09:30", periods=3, freq="5min")
    )
    missing = loader._check_missing_data(local, [day], [day], "5min")
    assert missing == [(day, day)]


def test_realtime_not_appended_for_weekly_or_history():
    loader = _loader(StubCalendar(trading=True))
    yesterday = (now_cn() - timedelta(days=5)).strftime("%Y-%m-%d")
    assert loader._should_append_realtime("w", today_str()) is False
    assert loader._should_append_realtime("m", today_str()) is False
    assert loader._should_append_realtime("d", yesterday) is False


def test_realtime_not_appended_on_holiday():
    loader = _loader(StubCalendar(trading=False))
    assert loader._should_append_realtime("d", today_str()) is False


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("09:00", False),
        ("10:00", True),
        ("12:00", False),   # 午休
        ("14:00", True),
        ("15:30", False),   # 收盘后
        ("20:00", False),
    ],
)
def test_realtime_only_in_continuous_trading_session(monkeypatch, clock, expected):
    fake = datetime.strptime(f"2024-01-02 {clock}", "%Y-%m-%d %H:%M")
    monkeypatch.setattr(loader_module, "now_cn", lambda: fake)
    monkeypatch.setattr(loader_module, "today_str", lambda: "2024-01-02")
    loader = _loader(StubCalendar(trading=True))
    assert loader._should_append_realtime("d", "2024-01-02") is expected


def test_adjust_anchor_prefers_database_factor():
    class StubFetcher:
        def latest_adj_factor(self, code):
            return 3.0

    loader = _loader()
    loader.fetcher = StubFetcher()
    frame = pd.DataFrame({"close": [10.0], "adj_factor": [1.0]})
    assert loader._adjust_anchor("600519", frame) == pytest.approx(3.0)


def test_adjust_anchor_falls_back_to_frame_factor():
    class StubFetcher:
        def latest_adj_factor(self, code):
            return None

    loader = _loader()
    loader.fetcher = StubFetcher()
    frame = pd.DataFrame({"close": [10.0], "adj_factor": [1.5]})
    assert loader._adjust_anchor("600519", frame) == pytest.approx(1.5)
    assert frame.attrs.get("adjust_anchor_source") == "frame"
