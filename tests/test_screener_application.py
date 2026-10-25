from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from screener.application.build_orchestrator import BuildOrchestrator
from screener.application.models import ChunkResult
from screener.application.screener_service import ScreenerService
from screener.config import ScreenerSettings
from screener.domain.build_state import BuildState
from screener.domain.errors import ScreenerNotBuilt
from screener.domain.models import CoverageReport, ScreenSpec


class FakeCalendar:
    def trading_days(self, start: date, end: date) -> list[date]:
        days = pd.bdate_range(start, end).date.tolist()
        return days

    def shift(self, day: date, bars: int) -> date:
        days = pd.bdate_range(day, periods=abs(bars) + 1)
        return days[0].date() if bars < 0 else days[-1].date()

    def latest_complete_trading_day(self, now: datetime | None = None) -> date:
        return date(2026, 10, 23)


class FakeBuildStateRepo:
    def __init__(self):
        self.states: dict[str, BuildState] = {}

    def get_latest(self, build_id: str):
        return self.states.get(build_id)

    def save(self, state: BuildState) -> None:
        self.states[state.build_id] = state

    def try_claim(self, build_id, owner, ttl_seconds=300, *, force=False):
        state = self.states.get(build_id)
        if state is None:
            return None
        return state if state.claim(owner, ttl_seconds, force=force) else None

    def list_recoverable(self, now=None):
        return [s for s in self.states.values() if s.can_resume(now)]

    def list_recent(self, limit=20):
        return list(self.states.values())[-limit:]


class FakeFactorStore:
    def __init__(self):
        self.rows: set[tuple[str, date]] = set()

    def read_daily(self, codes, start, end):
        data = [
            {"ts": datetime.combine(day, datetime.min.time()), "code": code, "close": 10.0}
            for code, day in self.rows
            if code in codes and start <= day <= end
        ]
        return pd.DataFrame(data)

    def coverage(self, codes, start, end):
        data = [
            {
                "ts": datetime.combine(day, datetime.min.time()),
                "code": code,
                "formula_version": "daily-v1",
                "is_partial": False,
                "full_ready": True,
            }
            for code, day in self.rows
            if code in codes and start <= day <= end
        ]
        return pd.DataFrame(data)

    def has_rows(self, codes, as_of):
        return self.count_rows(codes, as_of) > 0

    def count_rows(self, codes, as_of):
        return len({code for code, day in self.rows if code in codes and day == as_of})

    def wait_daily_visible(self, codes, start, end, timeout=3.0):
        return True

    def upsert_daily(self, frame):
        for _, row in frame.iterrows():
            day = pd.Timestamp(row["ts"]).date()
            self.rows.add((str(row["code"]), day))
        return len(frame)

    def latest_baseline_version(self, code, freq):
        return None

    def read_baseline(self, code, version, freq):
        return pd.DataFrame()

    def upsert_baseline(self, frame):
        return len(frame)

    def wait_baseline_visible(self, code, version, freq, timeout=3.0):
        return True


class FakeBackfill:
    def __init__(self, store: FakeFactorStore, calendar: FakeCalendar):
        self.store = store
        self.calendar = calendar
        self.calls = 0

    def ensure_daily(self, codes, start, end, *, on_chunk=None, deadline=None):
        self.calls += 1
        for code in codes:
            for day in self.calendar.trading_days(start, end):
                self.store.rows.add((code, day))
        report = CoverageReport(
            dataset="screener_daily",
            requested_codes=len(codes),
            available_codes=len(codes),
            requested_start=start,
            requested_end=end,
            complete=True,
        )
        if on_chunk:
            on_chunk(
                ChunkResult(
                    codes=list(codes),
                    start=start,
                    end=end,
                    rows_written=len(codes),
                    coverage=report,
                )
            )
        return report


class FakeMarket:
    def read_universe(self, market="MLCN", *, exclude_st=True, boards=None):
        return ["600519"]

    def read_ohlc(self, codes, start, end, freq="d"):
        return pd.DataFrame()

    def read_benchmark(self, start, end):
        return pd.DataFrame()

    def read_reference(self, codes):
        return pd.DataFrame()


class FakeQuery:
    def __init__(self):
        self.calls = 0

    def screen(self, spec):
        self.calls += 1
        return pd.DataFrame([{"code": "600519", "close": 10.0, "pct_chg": 1.0}])


def _service(backfill_calls: int = 0, store=None, build_state=None):
    settings = ScreenerSettings()
    calendar = FakeCalendar()
    store = store or FakeFactorStore()
    build_state = build_state or FakeBuildStateRepo()
    backfill = FakeBackfill(store, calendar)
    market = FakeMarket()
    orchestrator = BuildOrchestrator(
        build_state=build_state,
        backfill=backfill,
        factor_store=store,
        market=market,
        calendar=calendar,
        settings=settings,
        owner="test",
    )
    query = FakeQuery()
    service = ScreenerService(
        settings=settings,
        market=market,
        factor_store=store,
        query=query,
        build_state=build_state,
        calendar=calendar,
        orchestrator=orchestrator,
    )
    return service, backfill, store, build_state, query


def test_screen_builds_on_miss_and_hits_on_second_call():
    service, backfill, store, build_state, query = _service()
    spec = ScreenSpec.from_dict(
        {"as_of": "2026-10-23", "universe": {"codes": ["600519"]}, "limit": 10}
    )
    first = service.screen(spec)
    assert first.status == "DONE"
    assert first.quality == "FULL"
    assert backfill.calls == 1
    assert ("600519", date(2026, 10, 23)) in store.rows

    second = service.screen(
        ScreenSpec.from_dict(
            {"as_of": "2026-10-23", "universe": {"codes": ["600519"]}, "limit": 10}
        )
    )
    assert second.build_id == first.build_id
    assert backfill.calls == 1
    assert query.calls == 2


def test_partial_snapshot_is_returned_without_rebuild():
    service, backfill, store, build_state, query = _service()
    spec = ScreenSpec.from_dict(
        {"as_of": "2026-10-23", "universe": {"codes": ["600519"]}, "limit": 10}
    )
    first = service.screen(spec)
    state = build_state.get_latest(first.build_id)
    state.mark_partial("history short", coverage_ratio=0.5)
    build_state.save(state)
    before = backfill.calls

    second = service.screen(
        ScreenSpec.from_dict(
            {"as_of": "2026-10-23", "universe": {"codes": ["600519"]}, "limit": 10}
        )
    )
    assert second.status == "PARTIAL"
    assert second.quality == "PARTIAL"
    assert backfill.calls == before


def test_missing_snapshot_with_auto_build_disabled_raises():
    service, backfill, store, build_state, query = _service()
    with pytest.raises(ScreenerNotBuilt):
        service.screen(
            ScreenSpec.from_dict(
                {
                    "as_of": "2026-10-23",
                    "universe": {"codes": ["600519"]},
                    "auto_build": False,
                }
            )
        )
    assert backfill.calls == 0
