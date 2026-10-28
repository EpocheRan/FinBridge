"""screener 宽表快照同步。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class ScreenerSyncMixin:
    def _get_screener_app(self) -> Any:
        """惰性构建 screener 应用，避免纯日线/财务任务加载整个 screener。"""
        if self._screener_app_instance is None:
            if self._screener_app_factory is not None:
                self._screener_app_instance = self._screener_app_factory()
            else:
                from screener.bootstrap import build_screener_application

                self._screener_app_instance = build_screener_application(self.config)
        return self._screener_app_instance

    def sync_screener(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        codes: Sequence[str] | str | None = None,
        force: bool = False,
        app: Any | None = None,
    ) -> dict[str, Any]:
        """构建/修复指定日期范围的日频因子快照。"""
        from screener.domain.models import BuildRequest

        app = app or self._get_screener_app()
        selected_codes = normalize_selected_codes(codes)
        skipped_codes: list[str] = []
        if selected_codes is not None:
            a_share_codes = [code for code in selected_codes if is_a_share_code(code)]
            skipped_codes = [
                code for code in selected_codes if code not in set(a_share_codes)
            ]
            selected_codes = a_share_codes
            if not selected_codes:
                return {
                    "dataset": "screener",
                    "codes": [],
                    "done": True,
                    "status": "SKIPPED",
                    "reason": "指定的代码中没有 A 股，Screener 不构建 ETF/LOF",
                    "skipped_codes": skipped_codes,
                    "rows_written": 0,
                }

        if start_date is None and end_date is None:
            target = app.baseline_service.calendar.latest_complete_trading_day()
            start = end = target
        else:
            start, end = parse_date_range(start_date, end_date)

        request = BuildRequest(
            dataset="screener_daily",
            start=start,
            end=end,
            codes=selected_codes,
            force=force,
            formula_version=app.settings.daily.formula_version,
        )
        state = app.orchestrator.ensure_job(
            request,
            universe_key=request.universe_key,
            universe_payload=request.universe_payload,
            force=force,
        )
        status = state.status.value
        done = status in ("DONE", "PARTIAL")
        self.state.save(
            TASK_NAME,
            SCREENER_STATE,
            cursor=state.build_id,
            status=status,
            total=state.requested_codes,
            processed=state.built_codes,
            message=state.message,
            payload={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "codes": selected_codes,
                "force": bool(force),
            },
        )
        return {
            "dataset": "screener",
            "codes": selected_codes,
            "skipped_codes": skipped_codes,
            "done": done,
            "build_id": state.build_id,
            "status": status,
            "quality": state.quality.value,
            "coverage_ratio": state.coverage_ratio,
            "rows_written": state.rows_written,
            "requested_codes": state.requested_codes,
            "built_codes": state.built_codes,
            "window_start": state.window_start.isoformat()
            if state.window_start
            else None,
            "window_end": state.window_end.isoformat() if state.window_end else None,
            "message": state.message,
        }

    # ------------------------------------------------------------------ 分档
