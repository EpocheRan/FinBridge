"""分档（资金流）MCP 工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from data_interface.api.runtime import get_moneyflow_service
from data_interface.api.utils import dataframe_to_records, fail, from_exception, ok
from data_interface.moneyflow.models import RANK_FIELDS


def register_moneyflow_tools(mcp) -> None:
    @mcp.tool()
    def get_moneyflow(
        stock_code: str,
        start_date: str,
        end_date: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """获取个股日频分档（超大/大/中/小单 × 主动买/卖）。

        返回的每一行都带 ``source``，并在顶层回传 ``data_source``；
        金额单位元、量单位股。

        Args:
            stock_code: 6 位代码，如 "600519"。
            start_date: 开始日期 YYYY-MM-DD。
            end_date: 结束日期 YYYY-MM-DD。
            source: 指定数据源（如 "tushare.moneyflow"）；不传按配置优先级。
        """
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        if start_dt > end_dt:
            return fail("开始日期不能晚于结束日期", "InvalidDateRange", "请交换日期顺序。")
        try:
            service = get_moneyflow_service()
            frame = service.get_by_code(stock_code, start_date, end_date, source=source)
            if frame is None or frame.empty:
                return fail(
                    f"未找到 {stock_code} 在 {start_date} ~ {end_date} 的分档数据",
                    "NoMoneyflowData",
                    "请先调用 acquisition_run(datasets='moneyflow') 回补。",
                )
            records = dataframe_to_records(frame)
            data_source = str(frame["source"].iloc[-1]) if "source" in frame.columns else source
            return ok(
                f"获取 {stock_code} 分档数据 {len(records)} 条",
                {
                    "count": len(records),
                    "data_source": data_source,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_moneyflow_rank(
        trade_date: str | None = None,
        field: str = "main_amount",
        limit: int = 50,
        source: str | None = None,
        ascending: bool = False,
    ) -> dict[str, Any]:
        """按分档派生字段对某交易日做横截面排名。

        Args:
            trade_date: 交易日 YYYY-MM-DD；不传取该源最新日期。
            field: main_amount / net_mf_amount / super_large_net / large_net /
                small_net / buy_amount。
            limit: 返回条数，默认 50。
            source: 指定数据源；不传按配置优先级。
            ascending: true=升序（净流出在前）。
        """
        if field not in RANK_FIELDS:
            return fail(
                f"未知字段: {field}",
                "InvalidField",
                f"可选值: {', '.join(sorted(RANK_FIELDS))}",
            )
        try:
            service = get_moneyflow_service()
            if trade_date is None:
                latest = service.latest_ts(service.resolve_source(source))
                if latest is None:
                    return fail("分档表为空", "NoMoneyflowData", "请先回补分档数据。")
                trade_date = latest.strftime("%Y-%m-%d")
            frame = service.rank(
                trade_date,
                field=field,
                limit=limit,
                source=source,
                ascending=ascending,
            )
            if frame is None or frame.empty:
                return fail(
                    f"{trade_date} 没有分档数据",
                    "NoMoneyflowData",
                    "请确认该交易日已同步。",
                )
            records = dataframe_to_records(frame)
            data_source = str(frame["source"].iloc[0]) if "source" in frame.columns else source
            return ok(
                f"{trade_date} 按 {field} 排名返回 {len(records)} 条",
                {
                    "trade_date": trade_date,
                    "field": field,
                    "count": len(records),
                    "data_source": data_source,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_moneyflow_sources() -> dict[str, Any]:
        """列出分档数据源及其口径、覆盖区间、优先级。"""
        try:
            service = get_moneyflow_service()
            frame = service.sources()
            records = dataframe_to_records(frame)
            return ok(f"共 {len(records)} 个分档数据源", {"count": len(records), "data": records})
        except Exception as exc:
            return from_exception(exc)
