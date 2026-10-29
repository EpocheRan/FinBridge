"""披露日历 / 公告类 MCP 工具。"""

from __future__ import annotations

from typing import Any

from data_interface.api.runtime import get_disclosure_service
from data_interface.api.utils import fail, from_exception, ok


def _records(frame, limit: int | None = None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    data = frame.head(limit) if limit else frame
    records = data.to_dict("records")
    cleaned: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {}
        for key, value in record.items():
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat(sep=" ", timespec="seconds")
            elif value != value:  # NaN
                item[key] = None
            else:
                item[key] = value
        cleaned.append(item)
    return cleaned


def register_disclosure_tools(mcp) -> None:
    @mcp.tool()
    def get_disclosure_calendar(
        stock_code: str | None = None,
        report_date: str | None = None,
        as_of: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """获取预约披露日历（可看单只股票的历史预约/改期/实际披露）。

        Args:
            stock_code: 6 位代码；不传则返回该报告期全市场。
            report_date: 报告期（如 2026-09-30）。
            as_of: 快照日，只返回该日之前已知的预约信息。
            limit: 最多返回条数。
        """
        try:
            service = get_disclosure_service()
            frame = service.repository.calendar(stock_code, as_of=as_of, report_date=report_date)
            if frame is None or frame.empty:
                return fail(
                    "未找到预约披露记录",
                    "NoDisclosureData",
                    "请先运行 scripts/backfill_disclosure.py --calendar。",
                )
            if stock_code:
                frame = frame.sort_values("report_date", ascending=False)
            else:
                frame = frame.sort_values("appoint_date")
            return ok(
                f"获取披露日历 {min(len(frame), limit)} 条",
                {"count": int(len(frame)), "data": _records(frame, limit)},
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_upcoming_disclosures(days: int = 30, stock_code: str | None = None,
                                 as_of: str | None = None) -> dict[str, Any]:
        """未来 N 天内待披露的公司（事件驱动/财报博弈用）。

        Args:
            days: 未来天数，默认 30。
            stock_code: 只看某只股票；不传返回全市场。
            as_of: 基准日，默认今天。
        """
        try:
            service = get_disclosure_service()
            if stock_code:
                info = service.upcoming_for_stock(stock_code, days=days)
                if info is None:
                    return ok(f"{stock_code} 未来 {days} 天内无待披露事项", {"data": None})
                return ok(f"{stock_code} 距下次披露 {info['days_to_disclosure']} 天", info)
            frame = service.upcoming(days, as_of=as_of)
            if frame is None or frame.empty:
                return ok("未来无待披露记录", {"count": 0, "data": []})
            return ok(
                f"未来 {days} 天待披露 {len(frame)} 家",
                {"count": int(len(frame)), "data": _records(frame, 200)},
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_announcements(
        stock_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        sync_if_empty: bool = True,
    ) -> dict[str, Any]:
        """获取公告列表（含公告类型，可量化「减持/质押/中标」等事件）。

        Args:
            stock_code: 6 位代码。
            start_date: 起始日 YYYY-MM-DD。
            end_date: 结束日 YYYY-MM-DD。
            limit: 最多返回条数。
            sync_if_empty: 库内为空时是否即时从东财补一段。
        """
        try:
            service = get_disclosure_service()
            frame = service.announcements(
                stock_code, start=start_date, end=end_date, limit=limit
            )
            if (frame is None or frame.empty) and sync_if_empty:
                service.sync_announcements([stock_code], max_pages=2)
                frame = service.announcements(
                    stock_code, start=start_date, end=end_date, limit=limit
                )
            if frame is None or frame.empty:
                return fail(
                    "未找到公告",
                    "NoAnnouncementData",
                    "请运行 scripts/backfill_disclosure.py --codes <code>。",
                )
            return ok(
                f"获取公告 {len(frame)} 条",
                {"count": int(len(frame)), "data": _records(frame, limit)},
            )
        except Exception as exc:
            return from_exception(exc)
