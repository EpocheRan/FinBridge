"""acquisition MCP 工具注册测试（不触发同步）。"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from acquisition.api import register_acquisition_tools


def test_acquisition_tools_registered():
    mcp = FastMCP(name="test")
    register_acquisition_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"acquisition_run", "acquisition_status"} <= names
