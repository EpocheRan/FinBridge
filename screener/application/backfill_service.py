"""补齐编排：DB 优先，第三方兜底，写回后再计算。"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence

import pandas as pd

from screener.application.factor_service import FactorService
from screener.application.models import ChunkResult
from screener.application.ports import CalendarPort, MarketDataPort, SuspensionPort
from screener.config import BackfillSettings
from screener.domain.clock import now_local

logger = logging.getLogger("Screener.Backfill")
from screener.domain.coverage import classify_gaps, required_lookback_bars
from screener.domain.models import (
    CoverageReport,
    GapType,
    merge_date_ranges,
)
from screener.domain.suspension import Suspension, SuspensionStatus


class BackfillService:
    def __init__(
        self,
        *,
        market: MarketDataPort,
        calendar: CalendarPort,
        suspension: SuspensionPort,
        factor_service: FactorService,
        settings: BackfillSettings,
    ):
        self.market = market
        self.calendar = calendar
        self.suspension = suspension
        self.factor_service = factor_service
        self.settings = settings

    # ------------------------------------------------------------------ 原始
    def ensure_ohlc(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        freq: str = "d",
    ) -> CoverageReport:
        reports = self.market.ensure_ohlc(list(codes), start, end, freq)
        if not reports:
            return CoverageReport(
                dataset=f"ohlcv_{freq}",
                requested_codes=len(codes),
                requested_start=start,
                requested_end=end,
            )
        merged: CoverageReport | None = None
        for report in reports.values():
            merged = report if merged is None else merged.merge(report)
        if merged is None:
            return CoverageReport(dataset=f"ohlcv_{freq}")
        merged.requested_start = start
        merged.requested_end = end
        return merged

    def _safe_shift(self, day: date, bars: int) -> date:
        try:
            return self.calendar.shift(day, bars)
        except Exception:
            buffer_days = max(30, abs(bars) * 2 + 10)
            return day - timedelta(days=buffer_days) if bars < 0 else day + timedelta(days=buffer_days)

    # ------------------------------------------------------------------ 日频
    def ensure_daily(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        on_chunk: Callable[[ChunkResult], None] | None = None,
        deadline: float | None = None,
    ) -> CoverageReport:
        codes = list(dict.fromkeys(str(code) for code in codes if str(code)))
        if not codes:
            return CoverageReport(
                dataset="screener_daily",
                requested_start=start,
                requested_end=end,
                complete=True,
            )

        lookback = required_lookback_bars(self.factor_service.engine.config)
        cap = int(getattr(self.settings, "max_lookback_bars", 0) or 0)
        if cap and lookback > cap:
            logger.warning(
                "回看窗口 %s 超过配置上限 max_lookback_bars=%s，按上限截断"
                "（依赖更长历史的指标可能为空）",
                lookback,
                cap,
            )
            lookback = cap
        required_start = self._safe_shift(start, -lookback)
        if self.settings.fetch_benchmark:
            self.market.ensure_benchmark(required_start, end)
        self.market.ensure_reference(codes)

        expected = self.calendar.trading_days(start, end)
        result: CoverageReport | None = None
        processed: set[str] = set()
        chunks = [
            codes[index : index + self.settings.code_chunk_size]
            for index in range(0, len(codes), self.settings.code_chunk_size)
        ]
        for chunk in chunks:
            if deadline is not None and time.monotonic() > deadline:
                break
            suspensions = self.suspension.lookup_many(chunk, start, end)
            active = self._active_codes(chunk, expected, suspensions)
            if active:
                # 停牌/退市/NoData 的日期不算缺口，避免每次构建都重复回源
                blocked = {
                    code: _expand_suspensions(
                        suspensions.get(code, []), SuspensionStatus.SUSPENDED
                    )
                    | _expand_suspensions(
                        suspensions.get(code, []), SuspensionStatus.DELISTED
                    )
                    | _expand_suspensions(
                        suspensions.get(code, []), SuspensionStatus.NO_DATA
                    )
                    for code in active
                }
                self.market.ensure_ohlc(
                    active, required_start, end, "d", skip_dates=blocked
                )
                computed = self.factor_service.compute_chunk(
                    active, required_start, end, write_start=start
                )
            else:
                computed = None
            raw = self.market.read_ohlc(chunk, required_start, end, "d")
            factor_cov = self.factor_service.coverage(chunk, start, end)
            chunk_coverage = self._coverage_for_chunk(
                chunk, expected, raw, factor_cov, suspensions
            )
            result = (
                chunk_coverage
                if result is None
                else result.merge(chunk_coverage)
            )
            processed.update(chunk)
            if on_chunk is not None:
                on_chunk(
                    ChunkResult(
                        codes=list(chunk),
                        start=start,
                        end=end,
                        rows_written=computed.rows_written if computed else 0,
                        coverage=chunk_coverage,
                    )
                )

        if result is None:
            result = CoverageReport(
                dataset="screener_daily",
                requested_codes=len(codes),
                requested_start=start,
                requested_end=end,
            )

        remaining = [code for code in codes if code not in processed]
        if remaining:
            result.requested_codes += len(remaining)
            result.missing_ranges.update(
                {code: [r for r in merge_date_ranges(expected)] for code in remaining}
            )
            result.complete = False
        result.requested_start = start
        result.requested_end = end
        return result

    def _active_codes(
        self,
        codes: Sequence[str],
        expected: Sequence[date],
        suspensions_map: dict[str, list[Suspension]],
    ) -> list[str]:
        if not expected:
            return []
        active: list[str] = []
        for code in codes:
            suspensions = suspensions_map.get(code, [])
            blocked = (
                _expand_suspensions(suspensions, SuspensionStatus.SUSPENDED)
                | _expand_suspensions(suspensions, SuspensionStatus.DELISTED)
                | _expand_suspensions(suspensions, SuspensionStatus.NO_DATA)
            )
            if all(day in blocked for day in expected):
                continue
            active.append(code)
        return active

    def _coverage_for_chunk(
        self,
        codes: Sequence[str],
        expected: Sequence[date],
        raw: pd.DataFrame,
        factor_cov: pd.DataFrame,
        suspensions_map: dict[str, list[Suspension]] | None = None,
    ) -> CoverageReport:
        report = CoverageReport(
            dataset="screener_daily",
            requested_codes=len(codes),
            requested_start=expected[0] if expected else None,
            requested_end=expected[-1] if expected else None,
        )
        if not expected:
            report.complete = True
            report.available_codes = len(codes)
            return report

        raw_frame = raw.copy() if raw is not None else pd.DataFrame()
        if not raw_frame.empty and "ts" in raw_frame.columns:
            raw_frame["trade_date"] = pd.to_datetime(
                raw_frame["ts"], errors="coerce"
            ).dt.date
        else:
            raw_frame = pd.DataFrame(columns=["code", "trade_date"])

        cov_frame = factor_cov.copy() if factor_cov is not None else pd.DataFrame()
        if not cov_frame.empty and "ts" in cov_frame.columns:
            cov_frame["trade_date"] = pd.to_datetime(
                cov_frame["ts"], errors="coerce"
            ).dt.date
        else:
            cov_frame = pd.DataFrame(
                columns=[
                    "code",
                    "trade_date",
                    "formula_version",
                    "is_partial",
                    "full_ready",
                ]
            )

        expected_set = set(expected)
        for code in codes:
            raw_dates = set(
                raw_frame.loc[raw_frame["code"] == code, "trade_date"].dropna()
            )
            code_cov = cov_frame[cov_frame["code"] == code]
            factor_dates = set(code_cov["trade_date"].dropna())
            stale_dates = set(
                code_cov.loc[
                    code_cov["formula_version"]
                    != self.factor_service.engine.config.formula_version,
                    "trade_date",
                ].dropna()
            )
            suspensions = (suspensions_map or {}).get(
                code
            ) or self.suspension.lookup_range(code, expected[0], expected[-1])
            suspended_dates = _expand_suspensions(
                suspensions, SuspensionStatus.SUSPENDED
            )
            delisted_dates = _expand_suspensions(
                suspensions, SuspensionStatus.DELISTED
            )
            blocked_dates = suspended_dates | _expand_suspensions(
                suspensions, SuspensionStatus.NO_DATA
            )

            gaps = classify_gaps(
                code=code,
                expected_dates=expected,
                factor_dates=factor_dates,
                raw_dates=raw_dates,
                stale_dates=stale_dates,
                suspended_dates=blocked_dates,
                delisted_dates=delisted_dates,
            )
            blocking = [
                gap
                for gap in gaps
                if gap.gap_type
                in (
                    GapType.RAW_MISSING,
                    GapType.FACTOR_MISSING,
                    GapType.FACTOR_STALE,
                )
            ]
            if blocking:
                missing_dates = [day for gap in blocking for day in gap.dates]
                report.missing_ranges[code] = merge_date_ranges(missing_dates)
                for gap in blocking:
                    if gap.gap_type == GapType.RAW_MISSING and gap.dates:
                        self.mark_no_data(code, min(gap.dates), max(gap.dates))
                continue

            report.available_codes += 1
            effective = expected_set - blocked_dates - delisted_dates
            if not code_cov.empty and effective:
                partial = code_cov[
                    code_cov["trade_date"].isin(effective)
                ].get("is_partial")
                if partial is not None and bool(pd.Series(partial).fillna(False).any()):
                    report.partial_codes.append(code)

        report.complete = not report.missing_ranges
        return report

    # ------------------------------------------------------------------ 基线
    def ensure_baseline_data(
        self,
        codes: Sequence[str],
        as_of: date,
        freq: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        codes = [str(code) for code in codes]
        start = self._safe_shift(as_of, -lookback_days)
        self.market.ensure_ohlc(codes, start, as_of, freq)
        return self.market.read_ohlc(codes, start, as_of, freq)

    def mark_no_data(
        self,
        code: str,
        start: date,
        end: date,
        *,
        market: str = "MLCN",
        reason: str | None = None,
    ) -> None:
        retry_at = now_local() + timedelta(
            hours=self.settings.negative_cache_ttl_hours
        )
        self.suspension.upsert(
            [
                Suspension(
                    code=code,
                    market=market,
                    status=SuspensionStatus.NO_DATA,
                    start_date=start,
                    end_date=end,
                    retry_at=retry_at,
                    source="backfill",
                    reason=reason,
                )
            ]
        )


def _expand_suspensions(
    records: Iterable[Suspension], status: SuspensionStatus
) -> set[date]:
    result: set[date] = set()
    for item in records:
        if item.status != status:
            continue
        if status == SuspensionStatus.NO_DATA and not item.is_retry_blocked():
            continue
        if item.end_date is None:
            result.add(item.start_date)
            continue
        day = item.start_date
        while day <= item.end_date:
            result.add(day)
            day += timedelta(days=1)
    return result
