"""财务类 MCP 工具。"""

from __future__ import annotations

from typing import Any

from data_interface.api.runtime import get_financial_service
from data_interface.api.utils import fail, from_exception, ok


def register_financial_tools(mcp) -> None:
    @mcp.tool()
    def get_company_profile(stock_code: str, auto_sync: bool = True) -> dict[str, Any]:
        """获取公司基础信息、交易所/上市板、行业与板块归属。

        Args:
            stock_code: 6 位证券代码。
            auto_sync: 数据库无数据时是否自动从 Tushare/AkShare 同步。
        """
        try:
            profile = get_financial_service().get_profile(stock_code, auto_sync=auto_sync)
            return ok(f"成功获取 {stock_code} 公司资料", profile)
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_financial_statements(
        stock_code: str,
        statement_type: str = "IS",
        start_date: str | None = None,
        end_date: str | None = None,
        as_of: str | None = None,
        report_type: str | None = "consolidated",
        auto_sync: bool = True,
    ) -> dict[str, Any]:
        """获取利润表 / 资产负债表 / 现金流量表。

        Args:
            stock_code: 6 位证券代码。
            statement_type: IS=利润表，BS=资产负债表，CF=现金流量表，ALL=三表。
            start_date: 报告期开始日 YYYY-MM-DD。
            end_date: 报告期结束日 YYYY-MM-DD。
            as_of: Point-in-Time 查询日；只返回该日之前已披露的报表。
            report_type: consolidated=合并报表（默认），single_quarter=单季，None=全部。
            auto_sync: 无数据时是否自动同步。
        """
        try:
            service = get_financial_service()
            types = ["IS", "BS", "CF"] if statement_type.upper() == "ALL" else [statement_type.upper()]
            if any(item not in ("IS", "BS", "CF") for item in types):
                return fail("statement_type 必须是 IS / BS / CF / ALL", "InvalidArgument", "请使用 IS、BS、CF 或 ALL。")
            result = {}
            for item in types:
                rows = service.get_statements(
                    stock_code,
                    item,
                    start_date=start_date,
                    end_date=end_date,
                    as_of=as_of,
                    report_type=report_type,
                    auto_sync=auto_sync,
                )
                result[item] = {"count": len(rows), "data": rows}
            total = sum(item["count"] for item in result.values())
            return ok(f"成功获取 {stock_code} 财务报表（{total} 条）", result)
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_financial_indicators(
        stock_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        auto_sync: bool = True,
    ) -> dict[str, Any]:
        """获取盈利能力、成长能力、偿债能力等派生指标。"""
        try:
            rows = get_financial_service().get_indicators(
                stock_code,
                start_date=start_date,
                end_date=end_date,
                auto_sync=auto_sync,
            )
            return ok(f"成功获取 {stock_code} 财务指标", {"count": len(rows), "data": rows})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_valuation(
        stock_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        auto_sync: bool = True,
    ) -> dict[str, Any]:
        """获取每日估值：PE/PB/PS/PCF/PFCF/EV/EV_EBITDA 等。"""
        try:
            rows = get_financial_service().get_valuation(
                stock_code,
                start_date=start_date,
                end_date=end_date,
                auto_sync=auto_sync,
            )
            return ok(f"成功获取 {stock_code} 估值数据", {"count": len(rows), "data": rows})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_estimates(
        stock_code: str,
        fiscal_year: int | None = None,
        metric: str | None = None,
        auto_sync: bool = True,
    ) -> dict[str, Any]:
        """获取卖方一致预期（收入、净利润、EPS 等）。

        注意：Tushare 2120 积分下 ``report_rc`` 每天仅 10 次，超出会返回
        ``DailyQuotaExceededError``，此时系统会尝试 AkShare 兜底。
        """
        try:
            rows = get_financial_service().get_estimates(
                stock_code,
                fiscal_year=fiscal_year,
                metric=metric,
                auto_sync=auto_sync,
            )
            return ok(f"成功获取 {stock_code} 一致预期", {"count": len(rows), "data": rows})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_board_membership(stock_code: str) -> dict[str, Any]:
        """获取股票所属行业、概念、指数等板块。"""
        try:
            rows = get_financial_service().repository.get_board_membership(stock_code)
            return ok(f"成功获取 {stock_code} 板块归属", {"count": len(rows), "data": rows})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def sync_board_data(
        board_type: str = "industry",
        board_name: str | None = None,
    ) -> dict[str, Any]:
        """同步板块目录或单个板块成分股。

        Args:
            board_type: industry 或 concept。
            board_name: 为空时只同步板块目录；传入时同步该板块成分股。
        """
        try:
            service = get_financial_service()
            if board_name:
                count = service.sync_board_members(board_name, board_type)
                return ok(f"已同步板块「{board_name}」成分股", {"count": count})
            count = service.sync_board_catalog((board_type,))
            return ok(f"已同步 {board_type} 板块目录", {"count": count})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def sync_financial_data(
        stock_codes: str | list[str],
        start_year: int | None = None,
        end_year: int | None = None,
        include_estimates: bool = False,
    ) -> dict[str, Any]:
        """主动同步财务数据（按需调用，避免批量触发限流）。

        Args:
            stock_codes: 单个代码或代码列表。
            start_year: 起始财年，默认取配置 statement_start_year。
            end_year: 结束财年，默认当前年。
            include_estimates: 是否同步一致预期（Tushare 每天仅 10 次，谨慎开启）。
        """
        try:
            codes = [stock_codes] if isinstance(stock_codes, str) else list(stock_codes)
            results = [
                get_financial_service().sync_code(
                    code,
                    start_year=start_year,
                    end_year=end_year,
                    include_estimates=include_estimates,
                )
                for code in codes
            ]
            return ok(f"已同步 {len(codes)} 只标的财务数据", {"count": len(results), "data": results})
        except Exception as exc:
            return from_exception(exc)
