"""acquisition MCP 工具：整合同步的唯一入口。"""

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
        """运行整合同步（唯一入口，会阻塞到当前批次完成）。

        Args:
            datasets: 逗号分隔的数据集；不传则按依赖顺序全跑：
                trading_calendar, stock_basic, ohlcv_1d, benchmark, financial,
                valuation, moneyflow, margin_detail, top_list, block_trade,
                concept_meta, concept_daily, concept_member, concept_class,
                factors, screener。
            start_date / end_date: 日期范围，默认 2020-01-01 至今。
            codes: 指定股票/ETF 代码；不传表示全市场。
            boards: 指定板块代码（概念日线/成分用）。
        """
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
