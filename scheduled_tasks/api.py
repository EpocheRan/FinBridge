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
        codes: list[str] | str | None = None,
        max_trade_days: int | None = None,
        max_stocks: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool = True,
        include_funds: bool | None = None,
        include_benchmark: bool = True,
        include_screener: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """A 股行情/基准/财务/Screener 数据更新（供 Agent 心跳分批执行）。

        这个工具本身不包含定时器。Agent 每次心跳调用一次，处理一个有界批次，
        然后使用返回的 next_cursor / next_cursors 继续；直到 done=true。

        Args:
            dataset: daily=日线，benchmark=基准指数，financial=财务，
                screener=日频因子，all=四个阶段都执行。
            start_date: 开始日期 YYYY-MM-DD；不传时使用各数据集默认值。
                指定 codes 且不传 start_date 时，日线默认最近约 400 天、
                财务默认最近 2 年，避免误触发全历史回填。
            end_date: 结束日期 YYYY-MM-DD；不传时默认今天。
            codes: 指定股票/ETF 代码（如 ["600519","000001"] 或 "600519,000001"）；
                不传或传空表示全市场。指定代码时只更新这些代码，
                且不会覆盖全市场回填游标。
            max_trade_days: 日线本次最多处理多少个交易日，默认 20。
            max_stocks: 财务本次最多处理多少只股票，默认 15。
            cursor: 手动指定游标（日期或股票代码）；不传则从数据库状态续跑。
            reset: 是否重置游标，从头开始。
            include_valuation: 财务是否顺带同步每日估值，默认 True。
            include_funds: 日线是否同时拉取 ETF/LOF；
                不传时全市场按配置，指定代码时按代码类型自动判断。
            include_benchmark: dataset=all 时是否更新基准指数，默认 True。
            include_screener: dataset=all 时是否更新 Screener，默认 True。
            force: 是否强制刷新（财务 force、Screener 强制重建）。
        """
        try:
            result = _task_service().run(
                dataset=dataset,
                start_date=start_date,
                end_date=end_date,
                codes=codes,
                max_trade_days=max_trade_days,
                max_stocks=max_stocks,
                cursor=cursor,
                reset=reset,
                include_valuation=include_valuation,
                include_funds=include_funds,
                include_benchmark=include_benchmark,
                include_screener=include_screener,
                force=force,
            )
            return ok(
                "同步批次完成" if result.get("done") else "同步批次已执行，请继续调用",
                result,
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def scheduled_task_status() -> dict[str, Any]:
        """查看日线、基准、财务、Screener 任务的当前游标与进度。"""
        try:
            return ok("定时任务状态", _task_service().status())
        except Exception as exc:
            return from_exception(exc)
