"""acquisition MCP 工具：统一补齐 / 状态。"""

from __future__ import annotations

from datetime import date
from typing import Any

from data_interface.api.utils import fail, from_exception, ok

from .orchestrator import Scope, build_orchestrator


def _list(value: str | list[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(items) or None


def register_acquisition_tools(mcp) -> None:
    @mcp.tool()
    def acquisition_run(
        datasets: str | list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        codes: str | list[str] | None = None,
        boards: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """运行整合同步（会阻塞到当前批次完成）。"""
        try:
            start = date.fromisoformat(start_date) if start_date else date(2020, 1, 1)
            end = date.fromisoformat(end_date) if end_date else date.today()
            scope = Scope(
                start=start,
                end=end,
                codes=_list(codes),
                boards=_list(boards),
                datasets=_list(datasets),
            )
            result = build_orchestrator().run(scope)
            return ok(
                "整合同步完成" if not result["errors"] else "部分数据集失败",
                result,
                warnings=[item["error"] for item in result["errors"]],
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def acquisition_status() -> dict[str, Any]:
        """查看各数据集游标与状态。"""
        try:
            from data_interface.api.runtime import get_sync_service

            return ok("各数据集状态", get_sync_service().status())
        except Exception as exc:
            return from_exception(exc)
