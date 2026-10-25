"""MCP 响应与序列化工具。

统一响应信封（所有工具一致）：

```json
{"success": true,  "message": "...", "data": {...}, "warnings": ["..."]}
{"success": false, "message": "...", "error_details": {"type": "...", "suggestion": "..."}}
```

历史上行情/财务工具把负载放在 ``result``，持仓工具放在 ``data``，Agent 需要
分别处理；现在统一为 ``data``。
"""

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
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return json_safe(value.item())
    except ImportError:
        pass
    # 只对标量做 isna 判断：数组/DataFrame 会抛 "truth value is ambiguous"
    if not isinstance(value, (list, dict, tuple, set)) and not hasattr(value, "__len__"):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    return value


def ok(message: str, result: Any = None, *, warnings: list[str] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"success": True, "message": message}
    if result is not None:
        response["data"] = json_safe(result)
    if warnings:
        response["warnings"] = [str(item) for item in warnings if item]
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
        # 去掉空值，避免 error_details 里全是 null
        payload = {key: value for key, value in payload.items() if value is not None}
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
    if error_type == "SourceUnavailableError":
        return "数据源或数据库不可用。请确认 QuestDB 已启动、网络可达，然后重试。"
    return "请查看 logs/mcp_server.log 获取详细错误。"


def dataframe_to_records(
    df: pd.DataFrame,
    *,
    ts_field: str = "ts",
    drop_columns: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    frame = df.reset_index() if isinstance(df.index, pd.DatetimeIndex) else df.copy()
    if ts_field not in frame.columns and "index" in frame.columns:
        frame = frame.rename(columns={"index": ts_field})
    if drop_columns:
        frame = frame.drop(columns=[c for c in drop_columns if c in frame.columns], errors="ignore")
    records = frame.to_dict("records")
    return [json_safe(record) for record in records]
