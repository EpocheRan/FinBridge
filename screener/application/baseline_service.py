"""分时基线读取/构建编排。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from screener.application.build_orchestrator import BuildOrchestrator
from screener.application.ports import CalendarPort, FactorStorePort
from screener.config import ScreenerSettings
from screener.domain.models import CoverageReport


class BaselineService:
    def __init__(
        self,
        *,
        settings: ScreenerSettings,
        factor_store: FactorStorePort,
        calendar: CalendarPort,
        orchestrator: BuildOrchestrator,
    ):
        self.settings = settings
        self.factor_store = factor_store
        self.calendar = calendar
        self.orchestrator = orchestrator

    def get(
        self,
        code: str,
        *,
        as_of: date | None = None,
        freq: str | None = None,
        force: bool = False,
    ) -> pd.DataFrame:
        code = str(code).split(".")[0]
        as_of = as_of or self.calendar.latest_complete_trading_day()
        freq = freq or self.settings.baseline.freq
        latest = self.factor_store.latest_baseline_version(code, freq)
        max_age = int(getattr(self.settings.baseline, "max_age_days", 7))
        if (
            latest is not None
            and not force
            and (as_of - latest) <= timedelta(days=max_age)
        ):
            return self.factor_store.read_baseline(code, latest, freq)

        self.orchestrator.ensure_baseline_snapshot(
            code=code, as_of=as_of, freq=freq, force=force
        )
        version = self.factor_store.latest_baseline_version(code, freq) or as_of
        frame = self.factor_store.read_baseline(code, version, freq)
        if frame.empty and not force:
            # PARTIAL 状态可能没有写出任何行；基线读取允许一次显式强刷。
            self.orchestrator.ensure_baseline_snapshot(
                code=code, as_of=as_of, freq=freq, force=True
            )
            version = self.factor_store.latest_baseline_version(code, freq) or as_of
            frame = self.factor_store.read_baseline(code, version, freq)
        return frame

    def ensure(
        self,
        codes: list[str],
        *,
        as_of: date | None = None,
        freq: str | None = None,
        force: bool = False,
    ) -> CoverageReport:
        as_of = as_of or self.calendar.latest_complete_trading_day()
        freq = freq or self.settings.baseline.freq
        available = 0
        errors: dict[str, str] = {}
        for code in codes:
            try:
                self.orchestrator.ensure_baseline_snapshot(
                    code=code, as_of=as_of, freq=freq, force=force
                )
                available += 1
            except Exception as exc:
                errors[str(code)] = str(exc)
        return CoverageReport(
            dataset="intraday_baseline",
            requested_codes=len(codes),
            available_codes=available,
            requested_start=as_of,
            requested_end=as_of,
            errors=errors,
            complete=available == len(codes),
        )
