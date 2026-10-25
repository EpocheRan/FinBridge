"""筛股查询编排。"""

from __future__ import annotations

import time
from datetime import date
from typing import Sequence

import pandas as pd

from screener.application.build_orchestrator import BuildOrchestrator
from screener.application.ports import (
    BuildStatePort,
    CalendarPort,
    FactorStorePort,
    JobDispatcherPort,
    MarketDataPort,
    QueryPort,
)
from screener.config import ScreenerSettings
from screener.domain.errors import InvalidScreenSpec, ScreenerNotBuilt
from screener.domain.models import (
    BuildRequest,
    CoverageReport,
    ScreenResult,
    ScreenSpec,
    UniverseSpec,
)


class ScreenerService:
    def __init__(
        self,
        *,
        settings: ScreenerSettings,
        market: MarketDataPort,
        factor_store: FactorStorePort,
        query: QueryPort,
        build_state: BuildStatePort,
        calendar: CalendarPort,
        orchestrator: BuildOrchestrator,
        dispatcher: JobDispatcherPort | None = None,
    ):
        self.settings = settings
        self.market = market
        self.factor_store = factor_store
        self.query = query
        self.build_state = build_state
        self.calendar = calendar
        self.orchestrator = orchestrator
        self.dispatcher = dispatcher

    # ------------------------------------------------------------------ 查询
    def screen(self, spec: ScreenSpec) -> ScreenResult:
        self._apply_query_limits(spec)
        spec.validate()
        as_of, codes = self._resolve_universe(spec)
        spec.as_of = as_of

        deadline = (
            time.monotonic() + spec.budget_ms / 1000.0
            if spec.auto_build
            else None
        )
        state = self.orchestrator.ensure_snapshot(
            codes=codes,
            as_of=as_of,
            universe_key=self._universe_key(spec.universe, codes),
            universe_payload=spec.universe.payload,
            auto_build=spec.auto_build,
            deadline=deadline,
        )

        frame = self.query.screen(spec)
        rows = _records(frame)
        warnings: list[str] = []
        if state.status.value == "PARTIAL" and state.message:
            warnings.append(state.message)
        if state.status.value == "RUNNING":
            warnings.append("快照构建中，当前返回的是已有部分结果")
        if state.status.value == "FAILED" and state.last_error:
            warnings.append(state.last_error)

        return ScreenResult(
            as_of=as_of,
            rows=rows,
            total=len(rows),
            coverage=CoverageReport(
                dataset="screener_daily",
                requested_codes=state.requested_codes,
                available_codes=state.built_codes,
                requested_start=as_of,
                requested_end=as_of,
                complete=state.quality.value == "FULL",
            ),
            quality=state.quality.value,
            status=state.status.value,
            build_id=state.build_id,
            warnings=warnings,
            pending=state.status.value == "RUNNING",
        )

    def get_metrics(
        self,
        codes: Sequence[str] | None,
        start: date,
        end: date,
        *,
        auto_build: bool = True,
    ) -> pd.DataFrame:
        if isinstance(codes, str):
            codes = [codes]
        universe = UniverseSpec(codes=list(codes) if codes else None)
        resolved = (
            self.market.read_universe(
                universe.market,
                exclude_st=universe.exclude_st,
                boards=universe.boards,
            )
            if universe.codes is None
            else universe.normalized_codes() or []
        )
        if not resolved:
            return pd.DataFrame()
        if auto_build and len(resolved) > self.settings.backfill.max_sync_codes:
            raise InvalidScreenSpec(
                f"单次最多同步 {self.settings.backfill.max_sync_codes} 只，"
                "区间/全市场请使用 rebuild_screener 后台任务",
                codes=len(resolved),
                max_sync_codes=self.settings.backfill.max_sync_codes,
            )
        if auto_build:
            days = self.calendar.trading_days(start, end)
            if len(days) > self.settings.backfill.max_sync_days:
                raise InvalidScreenSpec(
                    "请求区间过大，请使用 rebuild_screener 后台任务",
                    max_sync_days=self.settings.backfill.max_sync_days,
                )
            request = BuildRequest(
                dataset="screener_daily",
                start=start,
                end=end,
                codes=resolved,
                freq="d",
                formula_version=self.settings.daily.formula_version,
            )
            self.orchestrator.ensure_job(
                request,
                universe_key=universe.key_for(resolved),
                universe_payload=universe.payload,
                deadline=time.monotonic()
                + self.settings.backfill.sync_budget_ms / 1000.0,
            )
        return self.factor_store.read_daily(resolved, start, end)

    # ------------------------------------------------------------------ 构建
    def rebuild(self, request: BuildRequest) -> str:
        if self.dispatcher is None:
            raise InvalidScreenSpec("当前未启用后台任务调度器")
        return self.dispatcher.submit(request)

    def status(self, build_id: str | None = None):
        if build_id:
            return self.build_state.get_latest(build_id)
        return self.build_state.list_recent()

    # ------------------------------------------------------------------ 内部
    def _apply_query_limits(self, spec: ScreenSpec) -> None:
        """把 ``query.default_limit`` / ``query.max_limit`` 真正生效。"""
        limit = spec.limit if spec.limit is not None else self.settings.query.default_limit
        max_limit = max(1, int(self.settings.query.max_limit))
        if limit > max_limit:
            raise InvalidScreenSpec(
                f"limit 不能超过 {max_limit}",
                limit=limit,
                max_limit=max_limit,
            )
        spec.limit = max(1, int(limit))

    @staticmethod
    def _universe_key(universe: UniverseSpec, codes: Sequence[str]) -> str:
        # 带上代码清单指纹：全市场新增上市/退市后 build_id 会变，自动重建
        return universe.key_for(codes)
    def _resolve_universe(self, spec: ScreenSpec) -> tuple[date, list[str]]:
        as_of = spec.as_of or self.calendar.latest_complete_trading_day()
        if spec.as_of is not None:
            # 显式 as_of：必须是交易日，且不能晚于最近一个完整交易日，
            # 否则会构建出一张"当天还没收盘"的 PARTIAL 快照，误导下游。
            if not self.calendar.trading_days(as_of, as_of):
                raise InvalidScreenSpec(
                    f"as_of={as_of.isoformat()} 不是交易日",
                    as_of=as_of.isoformat(),
                )
            latest = self.calendar.latest_complete_trading_day()
            if as_of > latest:
                raise InvalidScreenSpec(
                    f"as_of={as_of.isoformat()} 晚于最近完整交易日 {latest.isoformat()}",
                    as_of=as_of.isoformat(),
                    latest_complete=latest.isoformat(),
                )
        if spec.universe.codes is None:
            spec.universe.all_market = True
            codes = self.market.read_universe(
                spec.universe.market,
                exclude_st=spec.universe.exclude_st,
                boards=spec.universe.boards,
            )
        else:
            codes = spec.universe.normalized_codes() or []
        if not codes:
            raise ScreenerNotBuilt(
                "universe 为空，无法筛股",
                market=spec.universe.market,
                as_of=as_of.isoformat(),
            )
        return as_of, codes


def _records(frame: pd.DataFrame | None) -> list[dict]:
    if frame is None or frame.empty:
        return []
    cleaned = frame.copy()
    for column in cleaned.columns:
        if cleaned[column].dtype == "object":
            continue
        cleaned[column] = cleaned[column].where(pd.notna(cleaned[column]), None)
    return cleaned.to_dict("records")
