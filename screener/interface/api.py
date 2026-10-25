"""MCP tools for screener."""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

from data_interface.api.utils import dataframe_to_records, fail, from_exception, json_safe, ok
from screener.bootstrap import ScreenerApplication, build_screener_application
from screener.domain.errors import ScreenerError
from screener.domain.models import BuildRequest, ScreenSpec

logger = logging.getLogger("Screener.API")

_application: ScreenerApplication | None = None
_application_lock = threading.Lock()


def get_application() -> ScreenerApplication:
    global _application
    if _application is None:
        with _application_lock:
            if _application is None:
                _application = build_screener_application()
                _application.start()
    return _application


def register_screener_tools(mcp) -> None:
    @mcp.tool()
    def screen_stocks(
        spec: dict[str, Any] | None = None,
        as_of: str | None = None,
        codes: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort: list[dict[str, Any]] | None = None,
        limit: int = 100,
        offset: int = 0,
        auto_build: bool = True,
        allow_partial: bool = False,
        budget_ms: int = 5000,
    ) -> dict[str, Any]:
        """按日频因子筛选股票。

        Args:
            spec: 完整的 ScreenSpec 字典；提供后其余参数忽略。
            as_of: 交易日 YYYY-MM-DD；不传则取最近完整交易日。
            codes: 限定股票代码；不传则全市场。
            filters: 例如 [{"field":"beta_252","op":"between","value":[0.8,1.5]}]。
            sort: 例如 [{"field":"pct_chg","desc":true}]。
            limit: 返回条数，1~1000。
            offset: 分页偏移。
            auto_build: 未命中时是否内联构建。
            allow_partial: 是否返回 is_partial=true 的行。
            budget_ms: 内联构建等待预算。
        """
        try:
            payload = spec or {
                "as_of": as_of,
                "universe": {"codes": codes},
                "filters": filters or [],
                "sort": sort or [],
                "limit": limit,
                "offset": offset,
                "auto_build": auto_build,
                "allow_partial": allow_partial,
                "budget_ms": budget_ms,
            }
            screen_spec = ScreenSpec.from_dict(payload)
            result = get_application().service.screen(screen_spec)
            return ok(
                f"筛选完成，返回 {result.total} 条",
                result.to_dict(),
            )
        except ScreenerError as exc:
            return fail(
                exc.message,
                error_type=exc.error_type,
                suggestion="请检查参数，或先调用 rebuild_screener 构建快照。",
                **exc.extra,
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_screener_metrics(
        codes: list[str],
        start_date: str,
        end_date: str,
        auto_build: bool = True,
    ) -> dict[str, Any]:
        """查询指定股票的日频筛选因子。"""
        try:
            if isinstance(codes, str):
                codes = [codes]
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
            frame = get_application().service.get_metrics(
                codes, start, end, auto_build=auto_build
            )
            records = dataframe_to_records(frame)
            if not records:
                return fail(
                    "未找到 screener 因子",
                    error_type="ScreenerNotBuilt",
                    suggestion="请先调用 rebuild_screener，或开启 auto_build。",
                )
            return ok(f"获取 {len(records)} 条因子", {"data": records, "count": len(records)})
        except ScreenerError as exc:
            return fail(exc.message, error_type=exc.error_type, **exc.extra)
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_intraday_baseline(
        code: str,
        as_of: str | None = None,
        freq: str = "5m",
        force: bool = False,
    ) -> dict[str, Any]:
        """获取单只股票的分时量能基线。"""
        try:
            target = date.fromisoformat(as_of) if as_of else None
            frame = get_application().baseline_service.get(
                code, as_of=target, freq=freq, force=force
            )
            records = dataframe_to_records(frame)
            if not records:
                return fail(
                    f"未找到 {code} 的 {freq} 基线",
                    error_type="DataNotFoundError",
                    suggestion="请确认该股票有足够的分时历史。",
                )
            return ok(f"获取 {len(records)} 条基线", {"data": records, "count": len(records)})
        except ScreenerError as exc:
            return fail(exc.message, error_type=exc.error_type, **exc.extra)
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def rebuild_screener(
        dataset: str = "screener_daily",
        start_date: str | None = None,
        end_date: str | None = None,
        codes: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """提交重建任务，立即返回 build_id。"""
        try:
            app = get_application()
            if isinstance(codes, str):
                codes = [codes]
            normalized_dataset = (
                "screener_daily"
                if dataset in ("daily", "screener_daily")
                else dataset
            )
            if normalized_dataset == "intraday_baseline":
                start = date.fromisoformat(start_date) if start_date else app.baseline_service.calendar.latest_complete_trading_day()
                end = date.fromisoformat(end_date) if end_date else start
                if not codes:
                    return fail("intraday_baseline 需要 codes", "InvalidScreenSpec")
                app.baseline_service.ensure(codes, as_of=end, force=force)
                return ok("基线重建完成", {"codes": codes, "as_of": end.isoformat()})

            start = date.fromisoformat(start_date) if start_date else app.baseline_service.calendar.latest_complete_trading_day()
            end = date.fromisoformat(end_date) if end_date else start
            request = BuildRequest(
                dataset=normalized_dataset,
                start=start,
                end=end,
                codes=codes,
                force=force,
                formula_version=app.settings.daily.formula_version,
            )
            build_id = app.dispatcher.submit(request)
            return ok("重建任务已提交", {"build_id": build_id, "status": "PENDING"})
        except ScreenerError as exc:
            return fail(exc.message, error_type=exc.error_type, **exc.extra)
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_screener_status(build_id: str | None = None) -> dict[str, Any]:
        """查询构建状态；不传 build_id 时返回最近任务。"""
        try:
            app = get_application()
            states = app.service.status(build_id)
            if states is None:
                return fail("未找到构建状态", "DataNotFoundError")
            if isinstance(states, list):
                data = [_state_dict(state) for state in states]
            else:
                data = _state_dict(states)
            return ok("获取构建状态", data)
        except Exception as exc:
            return from_exception(exc)


def _state_dict(state) -> dict[str, Any]:
    return json_safe(
        {
            "build_id": state.build_id,
            "build_kind": state.build_kind.value,
            "dataset": state.dataset,
            "status": state.status.value,
            "quality": state.quality.value,
            "phase": state.phase.value,
            "scope_key": state.scope_key,
            "universe_key": state.universe_key,
            "window_start": state.window_start,
            "window_end": state.window_end,
            "progress_current": state.progress_current,
            "progress_total": state.progress_total,
            "rows_written": state.rows_written,
            "requested_codes": state.requested_codes,
            "built_codes": state.built_codes,
            "coverage_ratio": state.coverage_ratio,
            "attempts": state.attempts,
            "message": state.message,
            "last_error": state.last_error,
            "retry_at": state.retry_at,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "finished_at": state.finished_at,
        }
    )
