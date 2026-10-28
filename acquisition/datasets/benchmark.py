"""基准指数同步。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class BenchmarkSyncMixin:
    def sync_benchmark(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        app: Any | None = None,
    ) -> dict[str, Any]:
        """补齐基准指数日线（默认沪深 300，Beta 依赖）。"""
        app = app or self._get_screener_app()
        # 未指定日期时只保证最近约 400 个自然日（覆盖 252 交易日 Beta 窗口），
        # 避免每次都尝试回补指数早期缺失区间。
        default_start = self.settings.get("benchmark_start_date") or (
            date.fromisoformat(today_str()) - timedelta(days=400)
        ).isoformat()
        start, end = parse_date_range(
            start_date,
            end_date,
            default_start=default_start,
        )
        code = app.settings.benchmark_code
        report = app.service.market.ensure_benchmark(start, end)
        done = bool(report.complete)
        self.state.save(
            TASK_NAME,
            BENCHMARK_STATE,
            cursor=end.isoformat(),
            status="DONE" if done else "PARTIAL",
            total=1,
            processed=1 if done else 0,
            message=None if done else "基准指数存在缺口",
            payload={
                "code": code,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        return {
            "dataset": "benchmark",
            "code": code,
            "done": done,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "coverage": report.to_dict(),
        }

    # ------------------------------------------------------------------ Screener
