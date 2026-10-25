"""一次 build 的纯编排器。

不创建线程、不使用队列、不 sleep；只依赖 ports 与 domain 状态机。
真正执行进程/线程生命周期的是 infrastructure/job_queue.py。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Sequence

from screener.application.backfill_service import BackfillService
from screener.application.ports import (
    BuildStatePort,
    CalendarPort,
    FactorStorePort,
    MarketDataPort,
)
from screener.config import ScreenerSettings
from screener.domain.clock import now_local
from screener.domain.baseline import build_profile
from screener.domain.build_state import (
    BuildKind,
    BuildPhase,
    BuildState,
    make_build_id,
)
from screener.domain.errors import ScreenerNotBuilt
from screener.domain.models import BuildRequest, CoverageReport

logger = logging.getLogger("Screener.BuildOrchestrator")


class BuildOrchestrator:
    def __init__(
        self,
        *,
        build_state: BuildStatePort,
        backfill: BackfillService,
        factor_store: FactorStorePort,
        market: MarketDataPort,
        calendar: CalendarPort,
        settings: ScreenerSettings,
        owner: str | None = None,
    ):
        self.build_state = build_state
        self.backfill = backfill
        self.factor_store = factor_store
        self.market = market
        self.calendar = calendar
        self.settings = settings
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------ 快照
    def ensure_snapshot(
        self,
        *,
        codes: Sequence[str],
        as_of: date,
        universe_key: str,
        universe_payload: str,
        auto_build: bool = True,
        force: bool = False,
        deadline: float | None = None,
    ) -> BuildState:
        codes = [str(code) for code in codes]
        if not codes:
            raise ScreenerNotBuilt("没有可构建的股票代码", as_of=as_of.isoformat())

        formula_version = self.settings.daily.formula_version
        build_id = make_build_id(
            kind=BuildKind.SNAPSHOT,
            dataset="screener_daily",
            scope_key=as_of.isoformat(),
            universe_key=universe_key,
            freq="d",
            formula_version=formula_version,
        )
        state = self.build_state.get_latest(build_id)
        inconsistent = False

        if state and state.is_hit(formula_version) and not force:
            row_count = self.factor_store.count_rows(codes, as_of)
            if row_count >= max(0, state.built_codes):
                return state
            inconsistent = True
        if state and state.is_partial_hit(formula_version) and not force:
            return state
        if state and state.lease_active() and not force:
            return state

        if not auto_build and not force:
            raise ScreenerNotBuilt(
                "该交易日的 screener 快照尚未构建",
                as_of=as_of.isoformat(),
                missing_dates=[as_of.isoformat()],
                build_id=build_id,
            )

        if state is None:
            state = BuildState.new_snapshot(
                dataset="screener_daily",
                as_of=as_of,
                universe_key=universe_key,
                universe_payload=universe_payload,
                freq="d",
                formula_version=formula_version,
                requested_codes=len(codes),
                max_attempts=3,
            )
            self.build_state.save(state)

        claimed = self.build_state.try_claim(
            build_id,
            self.owner,
            ttl_seconds=self.settings.backfill.lock_ttl_seconds,
            force=force or inconsistent,
        )
        if claimed is None:
            latest = self.build_state.get_latest(build_id)
            if latest is None:
                raise ScreenerNotBuilt("构建状态不存在", build_id=build_id)
            return latest
        state = claimed
        state.requested_codes = len(codes)
        state.begin_phase(BuildPhase.RAW)
        self.build_state.save(state)

        try:
            def _progress(chunk) -> None:
                state.advance(
                    processed=len(chunk.codes),
                    total=len(codes),
                    rows_written=chunk.rows_written if chunk else 0,
                )
                self.build_state.save(state)

            coverage = self.backfill.ensure_daily(
                codes,
                as_of,
                as_of,
                on_chunk=_progress,
                deadline=deadline,
            )
            built_codes = self.factor_store.count_rows(codes, as_of)
            self._finalize_snapshot(state, coverage, built_codes)
        except Exception as exc:
            logger.exception("snapshot build failed: %s", build_id)
            state.mark_failed(str(exc))
            self.build_state.save(state)
            raise

        return state

    def _finalize_snapshot(
        self, state: BuildState, coverage: CoverageReport, built_codes: int
    ) -> None:
        state.coverage_ratio = coverage.coverage_ratio
        state.built_codes = built_codes
        if coverage.complete:
            state.mark_done(
                coverage_ratio=coverage.coverage_ratio,
                built_codes=built_codes,
            )
        else:
            reason = (
                f"{len(coverage.missing_ranges)} 个代码存在缺口"
                if coverage.missing_ranges
                else f"{len(coverage.partial_codes)} 个代码历史不足"
            )
            state.mark_partial(
                reason,
                coverage_ratio=coverage.coverage_ratio,
                retry_at=now_local() + timedelta(days=1),
            )
        self.build_state.save(state)

    # ------------------------------------------------------------------ 任务
    def ensure_job(
        self,
        request: BuildRequest,
        *,
        universe_key: str,
        universe_payload: str,
        force: bool = False,
        deadline: float | None = None,
    ) -> BuildState:
        codes = self._resolve_codes(request.codes)
        if not codes:
            raise ScreenerNotBuilt("没有可构建的股票代码", start=request.start.isoformat())

        formula_version = (
            self.settings.baseline.formula_version
            if request.dataset == "intraday_baseline"
            else self.settings.daily.formula_version
        )
        build_id = make_build_id(
            kind=BuildKind.JOB,
            dataset=request.dataset,
            scope_key=f"{request.start.isoformat()}:{request.end.isoformat()}",
            universe_key=universe_key,
            freq=request.freq,
            formula_version=formula_version,
        )
        state = self.build_state.get_latest(build_id)
        if state and state.is_hit(formula_version) and not force:
            return state
        if state and state.lease_active() and not force:
            return state

        if state is None:
            state = BuildState.new_job(
                dataset=request.dataset,
                start=request.start,
                end=request.end,
                universe_key=universe_key,
                universe_payload=universe_payload,
                freq=request.freq,
                formula_version=formula_version,
                requested_codes=len(codes),
                max_attempts=3,
            )
            self.build_state.save(state)

        claimed = self.build_state.try_claim(
            build_id,
            self.owner,
            ttl_seconds=self.settings.backfill.lock_ttl_seconds,
            force=force,
        )
        if claimed is None:
            return self.build_state.get_latest(build_id) or state
        state = claimed
        state.requested_codes = len(codes)

        dates = self.calendar.trading_days(request.start, request.end)
        if not dates:
            state.mark_partial("区间内没有交易日")
            self.build_state.save(state)
            return state

        # 按 20 个交易日切块，兼顾进度与请求次数。
        date_chunks = [
            dates[index : index + 20] for index in range(0, len(dates), 20)
        ]
        state.progress_total = len(date_chunks)
        state.begin_phase(BuildPhase.RAW)
        self.build_state.save(state)

        try:
            all_coverage: CoverageReport | None = None
            for chunk in date_chunks:
                if deadline is not None and time.monotonic() > deadline:
                    break
                coverage = self.backfill.ensure_daily(codes, chunk[0], chunk[-1])
                all_coverage = (
                    coverage
                    if all_coverage is None
                    else all_coverage.merge(coverage)
                )
                state.advance(
                    cursor_ts=datetime.combine(chunk[-1], datetime.min.time()),
                    cursor_code=None,
                    processed=1,
                    total=len(date_chunks),
                    rows_written=coverage.available_codes,
                )
                self.build_state.save(state)

            if all_coverage is None:
                state.mark_partial("没有完成任何日期块")
            elif all_coverage.complete:
                state.mark_done(
                    coverage_ratio=all_coverage.coverage_ratio,
                    built_codes=all_coverage.available_codes,
                )
            else:
                state.mark_partial(
                    "部分日期/代码存在缺口",
                    coverage_ratio=all_coverage.coverage_ratio,
                    retry_at=now_local() + timedelta(days=1),
                )
            self.build_state.save(state)
        except Exception as exc:
            logger.exception("job build failed: %s", build_id)
            state.mark_failed(str(exc))
            self.build_state.save(state)
            raise
        return state

    # ------------------------------------------------------------------ 基线
    def ensure_baseline_snapshot(
        self,
        *,
        code: str,
        as_of: date,
        freq: str,
        force: bool = False,
    ) -> BuildState:
        formula_version = self.settings.baseline.formula_version
        build_id = make_build_id(
            kind=BuildKind.SNAPSHOT,
            dataset="intraday_baseline",
            scope_key=as_of.isoformat(),
            universe_key=str(code),
            freq=freq,
            formula_version=formula_version,
        )
        state = self.build_state.get_latest(build_id)
        if state and state.is_hit(formula_version) and not force:
            return state
        if state and state.lease_active() and not force:
            return state

        if state is None:
            state = BuildState.new_snapshot(
                dataset="intraday_baseline",
                as_of=as_of,
                universe_key=str(code),
                universe_payload=json.dumps([str(code)]),
                freq=freq,
                formula_version=formula_version,
                requested_codes=1,
            )
            self.build_state.save(state)

        claimed = self.build_state.try_claim(
            build_id,
            self.owner,
            ttl_seconds=self.settings.backfill.lock_ttl_seconds,
            force=force,
        )
        if claimed is None:
            return self.build_state.get_latest(build_id) or state
        state = claimed

        try:
            bars = self.backfill.ensure_baseline_data(
                [code],
                as_of,
                freq,
                self.settings.baseline.lookback_days,
            )
            profile = build_profile(bars, self.settings.baseline)
            if profile.empty:
                state.mark_partial("没有可用的分时数据")
            else:
                profile = profile.copy()
                profile["as_of_date"] = datetime.combine(as_of, datetime.min.time())
                profile["market"] = "MLCN"
                profile["formula_version"] = formula_version
                profile["computed_at"] = now_local()
                written = self.factor_store.upsert_baseline(profile)
                visible = self.factor_store.wait_baseline_visible(
                    code, as_of, freq
                )
                if not visible:
                    logger.warning("基线写入后暂未对查询可见: %s", code)
                sample_days = int(profile["sample_days"].max())
                state.built_codes = 1
                state.rows_written = written
                state.coverage_ratio = min(
                    1.0,
                    sample_days / max(1, self.settings.baseline.lookback_days),
                )
                if sample_days >= self.settings.baseline.min_sample_days:
                    state.mark_done(coverage_ratio=state.coverage_ratio, built_codes=1)
                else:
                    state.mark_partial(
                        "分时样本不足",
                        coverage_ratio=state.coverage_ratio,
                        retry_at=now_local() + timedelta(days=1),
                    )
            self.build_state.save(state)
        except Exception as exc:
            logger.exception("baseline build failed: %s", build_id)
            state.mark_failed(str(exc))
            self.build_state.save(state)
            raise
        return state

    # ------------------------------------------------------------------ 恢复
    def recover(self) -> list[str]:
        recovered: list[str] = []
        for state in self.build_state.list_recoverable():
            try:
                codes = self._codes_from_state(state)
                if state.dataset == "intraday_baseline":
                    self.ensure_baseline_snapshot(
                        code=codes[0] if codes else state.universe_key,
                        as_of=state.window_start or self.calendar.latest_complete_trading_day(),
                        freq=state.freq,
                    )
                elif state.build_kind == BuildKind.SNAPSHOT:
                    self.ensure_snapshot(
                        codes=codes,
                        as_of=state.window_start
                        or self.calendar.latest_complete_trading_day(),
                        universe_key=state.universe_key,
                        universe_payload=state.universe_payload,
                        auto_build=True,
                    )
                else:
                    request = BuildRequest(
                        dataset=state.dataset,
                        start=state.window_start,
                        end=state.window_end,
                        codes=codes,
                        freq=state.freq,
                        formula_version=state.formula_version,
                    )
                    self.ensure_job(
                        request,
                        universe_key=state.universe_key,
                        universe_payload=state.universe_payload,
                    )
                recovered.append(state.build_id)
            except Exception as exc:
                logger.warning("恢复 build 失败 %s: %s", state.build_id, exc)
        return recovered

    # ------------------------------------------------------------------ 内部
    def _resolve_codes(self, codes: Sequence[str] | None) -> list[str]:
        if codes is not None:
            return [str(code).split(".")[0] for code in codes]
        return self.market.read_universe("MLCN", exclude_st=True)

    def _codes_from_state(self, state: BuildState) -> list[str]:
        if state.universe_payload:
            try:
                payload = json.loads(state.universe_payload)
                if isinstance(payload, list) and payload:
                    return [str(item).split(".")[0] for item in payload]
            except Exception:
                pass
        if state.universe_key == "ALL":
            return self.market.read_universe("MLCN", exclude_st=True)
        return []
