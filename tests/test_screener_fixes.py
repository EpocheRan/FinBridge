"""针对「观察问题 1-5」的回归测试。

1) 全市场 universe 指纹：代码清单变化要能触发重建
2) 死配置：source_priority / lookback 上限 / 查询 limit / 分块 真正生效
3) 停牌日期不算缺口（skip_dates），避免重复回源
4) 时间统一北京时间（domain.clock）
5) 非交易日 / 未收盘 as_of 直接报错
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from screener.application.backfill_service import BackfillService
from screener.application.screener_service import ScreenerService
from screener.config import ScreenerSettings
from screener.domain.clock import now_local
from screener.domain.errors import InvalidScreenSpec
from screener.domain.models import ScreenSpec, UniverseSpec
from screener.infrastructure.acquisition import MarketDataAdapter


# --------------------------------------------------------------------- 4) 时钟
def test_now_local_is_beijing_naive():
    value = now_local()
    assert value.tzinfo is None
    delta = value - datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(hours=7, minutes=50) < delta < timedelta(hours=8, minutes=10)


# ---------------------------------------------------------- 1) universe 指纹
def test_universe_key_includes_code_fingerprint():
    universe = UniverseSpec(market="MLCN", all_market=True)
    key_small = universe.key_for(["600519"])
    key_big = universe.key_for(["600519", "000001"])
    assert key_small != key_big
    assert key_small.startswith("ALL:MLCN:")


def test_explicit_codes_keep_their_own_key():
    universe = UniverseSpec(codes=["600519"])
    assert universe.key_for(["600519"]).startswith("CODES:MLCN:")


# --------------------------------------------------------------- 2) 查询参数
class FakeCalendar:
    def __init__(self, days: dict | None = None, latest: date | None = None):
        self._days = days or {}
        self._latest = latest or date(2026, 10, 23)

    def trading_days(self, start: date, end: date) -> list[date]:
        return self._days.get((start, end), [])

    def latest_complete_trading_day(self, now=None) -> date:
        return self._latest

    def shift(self, day: date, bars: int) -> date:
        return day + timedelta(days=bars)


def test_limit_defaults_and_caps_from_settings():
    service = object.__new__(ScreenerService)
    service.settings = ScreenerSettings.from_dict(
        {"query": {"default_limit": 50, "max_limit": 200}}
    )
    spec = ScreenSpec()
    service._apply_query_limits(spec)
    assert spec.limit == 50

    spec = ScreenSpec(limit=500)
    with pytest.raises(InvalidScreenSpec):
        service._apply_query_limits(spec)


# ------------------------------------------------------- 5) as_of 交易日校验
def _service_with_calendar(calendar: FakeCalendar):
    service = object.__new__(ScreenerService)
    service.settings = ScreenerSettings.from_dict({})
    service.calendar = calendar
    service.market = SimpleNamespace(read_universe=lambda *a, **k: ["600519"])
    return service


def test_non_trading_as_of_is_rejected():
    service = _service_with_calendar(FakeCalendar(days={}))
    spec = ScreenSpec(as_of=date(2026, 10, 24), universe=UniverseSpec(codes=["600519"]))
    with pytest.raises(InvalidScreenSpec):
        service._resolve_universe(spec)


def test_future_as_of_is_rejected():
    service = _service_with_calendar(
        FakeCalendar(days={(date(2099, 1, 2), date(2099, 1, 2)): [date(2099, 1, 2)]})
    )
    spec = ScreenSpec(as_of=date(2099, 1, 2), universe=UniverseSpec(codes=["600519"]))
    with pytest.raises(InvalidScreenSpec):
        service._resolve_universe(spec)


# ------------------------------------------------- 3) 停牌日期不算缺口 + 优先级
class FakeLoader:
    def __init__(self):
        self.calls: list[dict] = []

    def ohlc(self, codes, start, end, freq, **kwargs):
        self.calls.append(
            {"codes": list(codes), "start": start, "end": end, "freq": freq, **kwargs}
        )
        return {code: pd.DataFrame() for code in codes}


def _adapter(loader: FakeLoader, frame: pd.DataFrame, settings: dict):
    adapter = object.__new__(MarketDataAdapter)
    adapter.settings = ScreenerSettings.from_dict(settings)
    adapter.loader = loader
    adapter.calendar = FakeCalendar()
    adapter.read_ohlc = lambda codes, start, end, freq="d": frame
    return adapter


def test_suspended_dates_are_not_counted_as_missing():
    day = date(2026, 10, 23)
    frame = pd.DataFrame(
        [{"code": "600519", "ts": datetime(2026, 10, 23), "close": 1.0}]
    )
    loader = FakeLoader()
    adapter = _adapter(loader, frame, {})
    adapter.calendar = SimpleNamespace(
        get_trading_days=lambda start, end: [day.isoformat()],
        shift=lambda d, bars: d + timedelta(days=bars),
    )
    reports = adapter.ensure_ohlc(
        ["600519"], day, day, "d", skip_dates={"600519": {day}}
    )
    assert reports["600519"].complete is True
    assert loader.calls == []  # 停牌日不应触发回源


def test_missing_dates_trigger_fetch_with_configured_priority():
    day = date(2026, 10, 23)
    frame = pd.DataFrame(columns=["code", "ts", "close"])  # 本地为空
    loader = FakeLoader()
    adapter = _adapter(
        loader, frame, {"backfill": {"source_priority": ["baostock", "tushare"]}}
    )
    adapter.calendar = SimpleNamespace(
        get_trading_days=lambda start, end: [day.isoformat()],
        shift=lambda d, bars: d + timedelta(days=bars),
    )
    adapter.ensure_ohlc(["600519"], day, day, "d")
    assert loader.calls[0]["source_priority"] == ["baostock", "tushare"]


def test_long_range_is_chunked_by_daily_chunk_years():
    adapter = object.__new__(MarketDataAdapter)
    adapter.settings = ScreenerSettings.from_dict(
        {"backfill": {"daily_chunk_years": 1, "minute_chunk_days": 5}}
    )
    ranges = adapter._fetch_ranges(date(2024, 1, 1), date(2026, 6, 30), "d")
    assert len(ranges) == 3
    assert ranges[0] == (date(2024, 1, 1), date(2024, 12, 30))
    assert ranges[-1][1] == date(2026, 6, 30)


def test_minute_range_is_chunked_by_days():
    adapter = object.__new__(MarketDataAdapter)
    adapter.settings = ScreenerSettings.from_dict(
        {"backfill": {"minute_chunk_days": 5}}
    )
    ranges = adapter._fetch_ranges(date(2026, 10, 1), date(2026, 10, 12), "5m")
    assert ranges == [
        (date(2026, 10, 1), date(2026, 10, 5)),
        (date(2026, 10, 6), date(2026, 10, 10)),
        (date(2026, 10, 11), date(2026, 10, 12)),
    ]


# ------------------------------------------------------ 2) lookback 上限生效
class CapturingMarket:
    def __init__(self):
        self.calls: list[dict] = []

    def ensure_benchmark(self, start, end):
        return None

    def ensure_reference(self, codes):
        return None

    def ensure_ohlc(self, codes, start, end, freq="d", **kwargs):
        self.calls.append({"start": start, "end": end, **kwargs})
        return {}

    def read_ohlc(self, codes, start, end, freq="d"):
        return pd.DataFrame()

    def read_universe(self, *args, **kwargs):
        return []


class FakeFactorService:
    def __init__(self, lookback_config):
        self.engine = SimpleNamespace(config=lookback_config)

    def compute_chunk(self, codes, start, end, write_start=None):
        return SimpleNamespace(rows_written=0, frame=pd.DataFrame())

    def coverage(self, codes, start, end):
        return pd.DataFrame()


def test_lookback_is_capped_by_settings():
    market = CapturingMarket()
    service = object.__new__(BackfillService)
    service.market = market
    service.calendar = SimpleNamespace(
        trading_days=lambda start, end: [date(2026, 10, 23)],
        shift=lambda day, bars: day + timedelta(days=bars),
    )
    service.suspension = SimpleNamespace(
        lookup_many=lambda codes, start, end: {},
        lookup_range=lambda code, start, end: [],
        upsert=lambda items: 0,
    )
    service.factor_service = FakeFactorService(
        SimpleNamespace(
            beta_window=252,
            high_low_window=252,
            volatility_window=20,
            ma_windows=(5, 10, 20, 60, 120, 250),
            formula_version="daily-v1",
        )
    )
    service.settings = ScreenerSettings.from_dict(
        {"backfill": {"max_lookback_bars": 30, "code_chunk_size": 10}}
    ).backfill

    day = date(2026, 10, 23)
    service.ensure_daily(["600519"], day, day)

    assert market.calls, "应触发 ensure_ohlc"
    call = market.calls[0]
    # 上限 30 个交易日的回看 → 起点被截断到 day-30
    assert call["start"] == day - timedelta(days=30)
    assert "skip_dates" in call
