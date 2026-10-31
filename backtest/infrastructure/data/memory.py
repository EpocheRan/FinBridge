"""内存数据源：测试与离线回测。"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Sequence

import pandas as pd

from backtest.application.universe import UniverseSchedule, explicit_schedule
from backtest.domain.models import DataBundle, SecurityStatus, TradingCalendar, Universe
from backtest.domain.errors import ConfigError, DataError

from .snapshot import make_snapshot


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data = data.dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
    data = data.drop_duplicates(["symbol", "date"], keep="last")
    if "adj_factor" not in data.columns:
        data["adj_factor"] = 1.0
    data["adj_factor"] = pd.to_numeric(data["adj_factor"], errors="coerce").fillna(1.0)
    data["hfq_close"] = data["close"] * data["adj_factor"]
    return data.reset_index(drop=True)


def _calendar(frame: pd.DataFrame) -> TradingCalendar:
    dates = pd.to_datetime(frame["date"]).dropna().dt.normalize().unique()
    return TradingCalendar(tuple(sorted(pd.Timestamp(item) for item in dates)))


class MemoryDataSource:
    name = "memory"

    def __init__(
        self,
        frame: pd.DataFrame,
        benchmark: pd.DataFrame | None = None,
        security_status: SecurityStatus | None = None,
    ):
        self.frame = _normalize_frame(frame)
        self.benchmark = None if benchmark is None else benchmark.copy()
        self.security_status = security_status

    def resolve_universe(self, spec, start: date, end: date) -> UniverseSchedule:
        """内存源只支持 explicit；PIT 成分请用 questdb 或测试里的假数据源。"""
        kind = str(getattr(spec, "kind", "explicit")).strip().lower()
        if kind != "explicit":
            raise ConfigError(
                f"memory 数据源只支持 explicit universe，收到 {kind!r}"
            )
        codes = tuple(str(code) for code in getattr(spec, "codes", ()))
        if not codes:
            raise DataError("memory explicit universe 不能为空")
        return explicit_schedule(codes)

    def load(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        warmup: int = 0,
        freq: str = "d",
        extras: Sequence[str] = (),
    ) -> DataBundle:
        codes = tuple(sorted(set(str(item) for item in symbols)))
        start_ts = pd.Timestamp(start).normalize() - pd.Timedelta(days=max(0, int(warmup)) * 2 + 10)
        end_ts = pd.Timestamp(end).normalize()
        data = self.frame[
            self.frame["symbol"].isin(codes)
            & (self.frame["date"] >= start_ts)
            & (self.frame["date"] <= end_ts)
        ].copy()
        if data.empty:
            raise DataError(f"memory 数据为空: {codes} {start}..{end}")
        universe = Universe.from_codes(
            codes,
            as_of=pd.Timestamp(start).date(),
            fingerprint=hashlib.sha256("|".join(codes).encode()).hexdigest()[:16],
        )
        return DataBundle(
            frame=data.reset_index(drop=True),
            calendar=_calendar(data),
            snapshot=make_snapshot(self.name, data),
            universe=universe,
            security_status=self.security_status,
        )

    def load_benchmark(
        self, code: str, start: date, end: date, *, freq: str = "d"
    ) -> pd.Series | None:
        if self.benchmark is None or self.benchmark.empty:
            return None
        frame = self.benchmark.copy()
        if "date" not in frame.columns:
            frame = frame.reset_index().rename(columns={frame.columns[0]: "date"})
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        if "symbol" in frame.columns:
            frame = frame[frame["symbol"].astype(str) == str(code)]
        elif "code" in frame.columns:
            frame = frame[frame["code"].astype(str) == str(code)]
        frame = frame[
            (frame["date"] >= pd.Timestamp(start).normalize())
            & (frame["date"] <= pd.Timestamp(end).normalize())
        ]
        if frame.empty or "close" not in frame.columns:
            return None
        frame = frame.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
        return pd.Series(
            pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
            index=pd.DatetimeIndex(frame["date"]),
            name=str(code),
        ).dropna()
