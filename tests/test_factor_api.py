"""因子 MCP 工具离线测试（只查目录，不连 DB）。"""

from __future__ import annotations

import asyncio
import json

from fastmcp import FastMCP

from factor_engine.api import register_factor_tools


def _call(mcp, name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    return json.loads(result.content[0].text)


def test_factor_registry_tool_lists_builtins():
    mcp = FastMCP(name="test")
    register_factor_tools(mcp)

    payload = _call(mcp, "get_factor_registry", {"category": "momentum"})
    assert payload["success"] is True
    assert payload["data"]["count"] == 1
    item = payload["data"]["data"][0]
    assert item["factor_id"] == "momentum.ret"
    assert item["category"] == "momentum"
    assert item["source"]["dataset"] == "ohlcv_1d"
    assert item["window"] == 20
    assert item["freq"] == "1d"


def test_factor_registry_tool_window_filter():
    mcp = FastMCP(name="test")
    register_factor_tools(mcp)
    payload = _call(mcp, "get_factor_registry", {"window": 14})
    ids = {item["factor_id"] for item in payload["data"]["data"]}
    assert ids == {"technical.atr"}
