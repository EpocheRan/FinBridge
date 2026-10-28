"""Inline 后台任务调度。

V1 只支持进程内 ``ThreadPoolExecutor``；不实现 external DB 轮询 daemon。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from acquisition.build_orchestrator import BuildOrchestrator
from screener.application.ports import BuildStatePort, MarketDataPort
from screener.config import ScreenerSettings
from screener.domain.clock import now_local
from screener.domain.build_state import BuildKind, BuildState, BuildStatus
from screener.domain.models import BuildRequest, UniverseSpec

logger = logging.getLogger("Screener.JobQueue")


class ProcessWorker:
    """执行一个 build；只调用 application 编排器。"""

    def __init__(
        self,
        *,
        build_state: BuildStatePort,
        orchestrator: BuildOrchestrator,
        market: MarketDataPort,
    ):
        self.build_state = build_state
        self.orchestrator = orchestrator
        self.market = market

    def run(self, build_id: str) -> None:
        state = self.build_state.get_latest(build_id)
        deadline = time.monotonic() + 2.0
        while state is None and time.monotonic() < deadline:
            time.sleep(0.05)
            state = self.build_state.get_latest(build_id)
        if state is None or state.is_terminal():
            return
        try:
            codes = self._codes(state)
            if state.dataset == "intraday_baseline":
                code = codes[0] if codes else state.universe_key
                self.orchestrator.ensure_baseline_snapshot(
                    code=code,
                    as_of=state.window_start or now_local().date(),
                    freq=state.freq,
                )
            elif state.build_kind == BuildKind.SNAPSHOT:
                self.orchestrator.ensure_snapshot(
                    codes=codes,
                    as_of=state.window_start or now_local().date(),
                    universe_key=state.universe_key,
                    universe_payload=state.universe_payload,
                    auto_build=True,
                    force=True,
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
                self.orchestrator.ensure_job(
                    request,
                    universe_key=state.universe_key,
                    universe_payload=state.universe_payload,
                    force=True,
                )
        except Exception as exc:
            logger.exception("job failed: %s", build_id)
            state = self.build_state.get_latest(build_id) or state
            if not state.is_terminal():
                state.mark_failed(str(exc))
                self.build_state.save(state)

    def _codes(self, state: BuildState) -> list[str]:
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


class InProcessJobDispatcher:
    def __init__(
        self,
        *,
        build_state: BuildStatePort,
        orchestrator: BuildOrchestrator,
        market: MarketDataPort,
        settings: ScreenerSettings,
    ):
        self.build_state = build_state
        self.orchestrator = orchestrator
        self.market = market
        self.settings = settings
        self.worker = ProcessWorker(
            build_state=build_state, orchestrator=orchestrator, market=market
        )
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.settings.worker.max_workers,
                    thread_name_prefix="screener",
                )
        self.recover()

    def stop(self) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def submit(self, request: BuildRequest) -> str:
        codes = self._resolve_codes(request.codes)
        if not codes:
            raise ValueError("没有可构建的股票代码")
        universe = UniverseSpec(
            codes=None if request.codes is None else list(codes),
            all_market=request.codes is None,
        )
        formula_version = (
            self.settings.baseline.formula_version
            if request.dataset == "intraday_baseline"
            else self.settings.daily.formula_version
        )
        state = BuildState.new_job(
            dataset=request.dataset,
            start=request.start,
            end=request.end,
            universe_key=universe.universe_key,
            universe_payload=universe.payload,
            freq=request.freq,
            formula_version=formula_version,
            requested_codes=len(codes),
        )
        existing = self.build_state.get_latest(state.build_id)
        if (
            existing is not None
            and existing.status == BuildStatus.DONE
            and not request.force
        ):
            return existing.build_id
        if existing is None:
            self.build_state.save(state)
        elif request.force or existing.status in (
            BuildStatus.PARTIAL,
            BuildStatus.FAILED,
        ):
            state.created_at = existing.created_at
            self.build_state.save(state)
        self._ensure_started()
        assert self._executor is not None
        self._executor.submit(self.worker.run, state.build_id)
        return state.build_id

    def status(self, build_id: str) -> BuildState | None:
        return self.build_state.get_latest(build_id)

    def recover(self) -> None:
        self._ensure_started()
        assert self._executor is not None
        for state in self.build_state.list_recoverable():
            self._executor.submit(self.worker.run, state.build_id)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.settings.worker.max_workers,
                    thread_name_prefix="screener",
                )

    def _resolve_codes(self, codes: Sequence[str] | None) -> list[str]:
        if codes is not None:
            return [str(code).split(".")[0] for code in codes]
        return self.market.read_universe("MLCN", exclude_st=True)
