"""概念板块 MCP 工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from data_interface.api.runtime import get_concept_service
from data_interface.api.utils import dataframe_to_records, fail, from_exception, ok


def _valid_date(value: str | None) -> bool:
    if value is None:
        return True
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def register_concept_tools(mcp) -> None:
    @mcp.tool()
    def get_concept_boards(as_of: str | None = None) -> dict[str, Any]:
        """获取概念板块列表（默认最新快照）。

        Args:
            as_of: 只看该日期及之前的最近一份快照，格式 YYYY-MM-DD。
        """
        if not _valid_date(as_of):
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        try:
            frame = get_concept_service().list_boards(as_of=as_of)
            records = dataframe_to_records(frame)
            return ok(
                f"成功获取 {len(records)} 个概念板块",
                {
                    "count": len(records),
                    "as_of": as_of,
                    "data_source": str(frame["source"].iloc[0]) if not frame.empty else None,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_concept_members(board: str, as_of: str | None = None) -> dict[str, Any]:
        """获取概念板块成分股（PIT 快照）。

        Args:
            board: 板块代码（如 "308614"）或板块名称（如 "AI手机"）。
            as_of: 还原该日期及之前的最近一份成分快照；不传取最新。
        """
        if not _valid_date(as_of):
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        try:
            service = get_concept_service()
            frame = service.members(board, as_of=as_of)
            if frame is None or frame.empty:
                return fail(
                    f"未找到板块 {board} 的成分快照",
                    "NotFound",
                    "请确认板块代码/名称，或先运行 concept_member 同步任务。",
                )
            records = dataframe_to_records(frame)
            return ok(
                f"获取 {frame.attrs.get('board_name', board)} 成分股 {len(records)} 只",
                {
                    "count": len(records),
                    "board_code": frame.attrs.get("board_code", board),
                    "board_name": frame.attrs.get("board_name"),
                    "snapshot_ts": frame["ts"].iloc[0].isoformat() if "ts" in frame.columns else None,
                    "data_source": str(frame["source"].iloc[0]) if "source" in frame.columns else None,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_concept_ohlc(
        board: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """获取概念板块日线（OHLC + 成交量 + 成交额）。

        Args:
            board: 板块代码或名称。
            start_date: 开始日期 YYYY-MM-DD。
            end_date: 结束日期 YYYY-MM-DD。
        """
        if not _valid_date(start_date) or not _valid_date(end_date):
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        try:
            service = get_concept_service()
            matched = service.repository.find_board(board)
            if matched is None:
                return fail(f"未找到板块 {board}", "NotFound", "请先同步板块列表。")
            frame = service.daily(
                [str(matched["board_code"])], start_date, end_date
            )
            if frame is None or frame.empty:
                return fail(
                    f"板块 {matched['name']} 在 {start_date} ~ {end_date} 没有日线",
                    "NoConceptData",
                    "请先运行 concept_daily 同步任务。",
                )
            records = dataframe_to_records(frame)
            return ok(
                f"获取 {matched['name']} 日线 {len(records)} 条",
                {
                    "count": len(records),
                    "board_code": str(matched["board_code"]),
                    "board_name": str(matched["name"]),
                    "data_source": str(frame["source"].iloc[-1]) if "source" in frame.columns else None,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_concept_classification(
        keyword: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """获取 Tushare 概念分类树（可按键词过滤）。

        Args:
            keyword: 匹配概念名或三级分类名，如 "人工智能"。
            as_of: 该日期及之前的最近一份分类快照。
        """
        if not _valid_date(as_of):
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        try:
            frame = get_concept_service().classification(as_of=as_of)
            if frame is None or frame.empty:
                return fail("分类表为空", "NoConceptData", "请先运行 concept_class 同步任务。")
            if keyword:
                text = str(keyword)
                mask = pd.Series(False, index=frame.index)
                for column in ("name", "class1_name", "class2_name", "class3_name"):
                    if column in frame.columns:
                        mask = mask | frame[column].astype(str).str.contains(text, na=False)
                frame = frame[mask]
            records = dataframe_to_records(frame)
            return ok(
                f"获取概念分类 {len(records)} 条",
                {
                    "count": len(records),
                    "keyword": keyword,
                    "data_source": str(frame["source"].iloc[0]) if not frame.empty and "source" in frame.columns else None,
                    "data": records,
                },
            )
        except Exception as exc:
            return from_exception(exc)
