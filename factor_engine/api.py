"""因子库 MCP 工具。"""

from __future__ import annotations

from typing import Any

from data_interface.api.utils import dataframe_to_records, fail, from_exception, ok

from .service import FactorEngineService


def _ids(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def register_factor_tools(mcp) -> None:
    @mcp.tool()
    def get_factor_registry(
        category: str | None = None,
        source: str | None = None,
        freq: str | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        """查询因子目录（分类 / 数据来源 / 频率 / 窗口）。"""
        try:
            service = FactorEngineService()
            specs = service.list_factors()
            if category:
                specs = [item for item in specs if item.category.value == category]
            if source:
                specs = [item for item in specs if item.source.dataset == source]
            if freq:
                specs = [item for item in specs if item.freq == freq]
            if window is not None:
                specs = [item for item in specs if item.window == int(window)]
            payload = [item.to_payload() for item in specs]
            return ok(f"共 {len(payload)} 个因子", {"count": len(payload), "data": payload})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_factor_values(
        factor_ids: str | list[str],
        start_date: str,
        end_date: str,
        codes: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """查询因子长表值（factor_value_daily）。"""
        ids = _ids(factor_ids)
        if not ids:
            return fail("factor_ids 不能为空", "InvalidArgument")
        try:
            service = FactorEngineService()
            frame = service.read(ids, start_date, end_date, _ids(codes) or None)
            records = dataframe_to_records(frame)
            if not records:
                return fail("没有因子值", "NoFactorData", "请先运行 factor_engine build。")
            return ok(
                f"获取 {len(records)} 条因子值",
                {"count": len(records), "data": records},
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_factor_latest(
        factor_ids: str | list[str],
        codes: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """查询每个 code × factor 的最新值（factor_value_latest）。"""
        ids = _ids(factor_ids)
        if not ids:
            return fail("factor_ids 不能为空", "InvalidArgument")
        try:
            service = FactorEngineService()
            frame = service.read_latest(ids, _ids(codes) or None)
            records = dataframe_to_records(frame)
            if not records:
                return fail("没有最新因子值", "NoFactorData", "请先运行 factor_engine build。")
            return ok(
                f"获取 {len(records)} 条最新因子值",
                {"count": len(records), "data": records},
            )
        except Exception as exc:
            return from_exception(exc)
