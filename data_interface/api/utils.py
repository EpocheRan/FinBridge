"""MCP 响应与序列化工具。"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd

from data_interface.common.exceptions import DataInterfaceError


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except ImportError:
        pass
    if not isinstance(value, (list, dict, tuple)) and pd.isna(value):
        return None
    return value


def ok(message: str, result: Any = None) -> dict[str, Any]:
    response = {"success": True, "message": message}
    if result is not None:
        response["result"] = json_safe(result)
    return response


def fail(message: str, error_type: str = "SystemError", suggestion: str | None = None, **extra) -> dict[str, Any]:
    return {
        "success": False,
        "message": message,
        "error_details": {
            "type": error_type,
            "suggestion": suggestion or "请检查参数或稍后重试。",
            **extra,
        },
    }


def from_exception(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DataInterfaceError):
        payload = exc.to_dict()
        error_type = payload.pop("error_type", "DataInterfaceError")
        message = payload.pop("message", str(exc))
        payload.pop("source", None)
        return fail(
            message,
            error_type=error_type,
            suggestion=_suggestion_for(error_type),
            **payload,
        )
    return fail(
        f"{type(exc).__name__}: {exc}",
        error_type="SystemError",
        suggestion="请查看服务日志，确认数据源/数据库可用后重试。",
    )


def _suggestion_for(error_type: str) -> str:
    if error_type in ("RateLimitExceededError", "DailyQuotaExceededError"):
        return "已触发数据源限流。请降低请求频率，或等待配额恢复后再试；不要在全市场循环中高频调用。"
    if error_type == "PermissionDeniedError":
        return "当前 Tushare 积分不足或接口未开通。请检查 config.json 中的积分/权限，或改用 AkShare 兜底。"
    if error_type == "DataNotFoundError":
        return "请确认代码/报告期是否正确，或先调用 sync_financial_data 同步数据。"
    if error_type == "ConfigurationError":
        return "请检查 config.json 的 questdb / tushare / financial 配置。"
    return "请查看 logs/mcp_server.log 获取详细错误。"


def dataframe_to_records(df: pd.DataFrame, *, ts_field: str = "ts") -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    frame = df.reset_index() if isinstance(df.index, pd.DatetimeIndex) else df.copy()
    records = frame.to_dict("records")
    return [json_safe(record) for record in records]
