"""定时任务 MCP 工具。"""

from __future__ import annotations

from typing import Any

from data_interface.api.runtime import get_sync_service
from data_interface.api.utils import from_exception, ok


def _task_service():
    # 与行情/财务共用同一份 DB 连接与 Tushare 限流器
    return get_sync_service()


def register_scheduled_tools(mcp) -> None:
    @mcp.tool()
    def scheduled_sync_a_shares(
        dataset: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        max_stocks: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        include_funds: bool | None = None,
    ) -> dict[str, Any]:
        """A 股全量日线/财务数据同步（供 Agent 心跳分批执行）。

        这个工具本身不包含定时器。Agent 每次心跳调用一次，处理一个有界批次，
        然后使用返回的 next_cursor 继续；直到 done=true。

        Args:
            dataset: daily=日线，financial=财务，all=两者都执行。
            start_date: 自定义开始日期 YYYY-MM-DD；财务会取年份。
            end_date: 自定义结束日期 YYYY-MM-DD。
            max_trade_days: 本次最多处理多少个交易日，默认 20。
            max_stocks: 本次最多处理多少只股票的财务，默认 15。
            cursor: 手动指定游标（日期或股票代码）；不传则从数据库状态续跑。
            reset: 是否重置游标，从头开始。
            include_valuation: 财务任务是否顺带同步每日估值。
            include_funds: 日线任务是否同时拉取 ETF/LOF（默认取配置，true）。
        """
        try:
            result = _task_service().run(
                dataset=dataset,
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                max_stocks=max_stocks,
                cursor=cursor,
                reset=reset,
                include_valuation=include_valuation,
                include_funds=include_funds,
            )
            return ok(
                "同步批次完成" if result.get("done") else "同步批次已执行，请继续调用",
                result,
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def scheduled_task_status() -> dict[str, Any]:
        """查看日线和财务同步任务的当前游标与进度。"""
        try:
            return ok("定时任务状态", _task_service().status())
        except Exception as exc:
            return from_exception(exc)
