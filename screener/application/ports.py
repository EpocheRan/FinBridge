"""Application 端口（Protocol）。

application 只依赖这些抽象；具体实现由 infrastructure 提供，
bootstrap 负责注入。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Mapping, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

    from screener.domain.build_state import BuildState
    from screener.domain.models import (
        BuildRequest,
        CoverageReport,
        ScreenSpec,
    )
    from screener.domain.suspension import Suspension


class MarketDataPort(Protocol):
    """行情、基准、参考数据访问。"""

    def ensure_ohlc(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        freq: str = "d",
        *,
        skip_dates: "Mapping[str, set[date]] | None" = None,
    ) -> dict[str, "CoverageReport"]:
        ...

    def read_ohlc(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        freq: str = "d",
    ) -> "pd.DataFrame":
        ...

    def ensure_benchmark(self, start: date, end: date) -> "CoverageReport":
        ...

    def read_benchmark(self, start: date, end: date) -> "pd.DataFrame":
        ...

    def ensure_reference(self, codes: Sequence[str]) -> "CoverageReport":
        ...

    def read_reference(self, codes: Sequence[str]) -> "pd.DataFrame":
        ...

    def read_universe(
        self,
        market: str = "MLCN",
        *,
        exclude_st: bool = True,
        boards: Sequence[str] | None = None,
    ) -> list[str]:
        ...


class CalendarPort(Protocol):
    def trading_days(self, start: date, end: date) -> list[date]:
        ...

    def shift(self, day: date, bars: int) -> date:
        ...

    def latest_complete_trading_day(self, now: datetime | None = None) -> date:
        ...


class FactorStorePort(Protocol):
    def read_daily(
        self, codes: Sequence[str], start: date, end: date
    ) -> "pd.DataFrame":
        ...

    def coverage(
        self, codes: Sequence[str], start: date, end: date
    ) -> "pd.DataFrame":
        ...

    def has_rows(self, codes: Sequence[str], as_of: date) -> bool:
        ...

    def count_rows(self, codes: Sequence[str], as_of: date) -> int:
        ...

    def wait_daily_visible(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        timeout: float = 3.0,
    ) -> bool:
        ...

    def upsert_daily(self, frame: "pd.DataFrame") -> int:
        ...

    def latest_baseline_version(self, code: str, freq: str) -> date | None:
        ...

    def read_baseline(
        self, code: str, version: date, freq: str
    ) -> "pd.DataFrame":
        ...

    def upsert_baseline(self, frame: "pd.DataFrame") -> int:
        ...

    def wait_baseline_visible(
        self, code: str, version: date, freq: str, timeout: float = 3.0
    ) -> bool:
        ...


class BuildStatePort(Protocol):
    def get_latest(self, build_id: str) -> "BuildState | None":
        ...

    def save(self, state: "BuildState") -> None:
        ...

    def try_claim(
        self,
        build_id: str,
        owner: str,
        ttl_seconds: int = 300,
        *,
        force: bool = False,
    ) -> "BuildState | None":
        ...

    def list_recoverable(self, now: datetime | None = None) -> list["BuildState"]:
        ...

    def list_recent(self, limit: int = 20) -> list["BuildState"]:
        ...


class SuspensionPort(Protocol):
    def lookup(
        self, code: str, day: date, market: str = "MLCN"
    ) -> "Suspension | None":
        ...

    def lookup_range(
        self, code: str, start: date, end: date, market: str = "MLCN"
    ) -> list["Suspension"]:
        ...

    def lookup_many(
        self, codes: Sequence[str], start: date, end: date, market: str = "MLCN"
    ) -> dict[str, list["Suspension"]]:
        ...

    def upsert(self, items: Iterable["Suspension"]) -> int:
        ...


class QueryPort(Protocol):
    def screen(self, spec: "ScreenSpec") -> "pd.DataFrame":
        ...


class JobDispatcherPort(Protocol):
    def submit(self, request: "BuildRequest") -> str:
        ...

    def status(self, build_id: str) -> "BuildState | None":
        ...
