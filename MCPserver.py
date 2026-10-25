"""FinBridge MCP Server.

职责边界：
- 只负责组装 MCP 工具与启动服务。
- 行情/财务逻辑在 ``data_interface``。
- 持仓逻辑在 ``position``。
- 不在启动时检查或创建数据库表；表由 ``scripts/init_database.py`` 或
  Docker ``db-init`` 服务完成。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

from fastmcp import FastMCP

from data_interface.api.financial_tools import register_financial_tools
from data_interface.api.market_tools import register_market_tools
from data_interface.common.config import get_config, project_path
from data_interface.market.loader import MarketDataLoader
from position import PositionAgentInterface

CONFIG = get_config()
LOGGING_CONFIG = CONFIG.get("logging", {})
SERVER_CONFIG = CONFIG.get("server", {})


def setup_logging() -> None:
    log_dir = project_path(LOGGING_CONFIG.get("directory", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOGGING_CONFIG.get("file", "mcp_server.log")

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOGGING_CONFIG.get("level", "INFO").upper(), logging.INFO))
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(LOGGING_CONFIG.get("max_bytes", 10 * 1024 * 1024)),
        backupCount=int(LOGGING_CONFIG.get("backup_count", 7)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.CRITICAL)
    console.setFormatter(formatter)
    root.addHandler(console)

    for noisy in ("DataInterface", "PositionManager", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger("MCPServer")

mcp = FastMCP(name=SERVER_CONFIG.get("name", "QuantDataServer"))
market_loader = MarketDataLoader()
position_interface = PositionAgentInterface(data_loader=market_loader)

register_market_tools(mcp)
register_financial_tools(mcp)


# ---------------------------------------------------------------------------
# 持仓工具（保留原有 Agent 交互契约）
# ---------------------------------------------------------------------------
@mcp.tool()
async def pm_create_portfolio(
    name: str,
    initial_cash: float,
    reuse_existing: bool = None,
    tickers: list[str] = None,
) -> dict[str, Any]:
    """Create a new investment portfolio, or confirm reuse of a similar existing one.

    Args:
        name: Portfolio name.
        initial_cash: Initial capital.
        reuse_existing: true=复用相似组合；false=强制新建；不传=自动探测并返回确认提示。
        tickers: 该组合计划交易的标的（可选，用于同标的相似度检测）。
    """
    return position_interface.create_portfolio(
        name, initial_cash, reuse_existing, tickers
    )


@mcp.tool()
async def pm_list_portfolios() -> dict[str, Any]:
    """List all available investment portfolios."""
    return position_interface.list_portfolios()


@mcp.tool()
async def pm_delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    """Delete a portfolio and all its data."""
    return position_interface.delete_portfolio(portfolio_id)


@mcp.tool()
async def pm_modify_portfolio(
    portfolio_id: str, name: str = None, cash: float = None
) -> dict[str, Any]:
    """Modify portfolio name or cash balance."""
    return position_interface.modify_portfolio(portfolio_id, name, cash)


@mcp.tool()
async def pm_modify_position(
    pos_id: str,
    name: str = None,
    quantity: float = None,
    n_factor: float = None,
    risk_per_trade: float = None,
    mode_params: dict = None,
) -> dict[str, Any]:
    """Modify position name, quantity, or risk parameters."""
    merged = dict(mode_params or {})
    if n_factor is not None:
        merged["n_factor"] = n_factor
    if risk_per_trade is not None:
        merged["risk_per_trade"] = risk_per_trade
    return position_interface.modify_position(pos_id, name, quantity, merged or None)


@mcp.tool()
async def pm_get_portfolio_status(portfolio_id: str) -> dict[str, Any]:
    """Get the current status of the portfolio."""
    return position_interface.get_portfolio_status(portfolio_id)


@mcp.tool()
async def pm_list_modes() -> dict[str, Any]:
    """List all available risk management and position sizing modes."""
    return position_interface.get_available_modes()


@mcp.tool()
async def pm_open_position(
    portfolio_id: str,
    ticker: str,
    name: str,
    mode: str,
    initial_atr: float = None,
    n_factor: float = None,
    risk_per_trade: float = None,
) -> dict[str, Any]:
    """Open a new tracking position."""
    kwargs = {}
    if initial_atr is not None:
        kwargs["initial_atr"] = initial_atr
    if n_factor is not None:
        kwargs["n_factor"] = n_factor
    if risk_per_trade is not None:
        kwargs["risk_per_trade"] = risk_per_trade
    return position_interface.open_position(portfolio_id, ticker, name, mode, **kwargs)


@mcp.tool()
async def pm_execute_trade(
    portfolio_id: str, ticker: str, action: str, quantity: float, price: float
) -> dict[str, Any]:
    """Record a buy or sell trade."""
    return position_interface.execute_trade(
        portfolio_id, ticker, action, quantity, price
    )


@mcp.tool()
async def pm_close_position(
    portfolio_id: str, ticker: str, price: float
) -> dict[str, Any]:
    """Fully close a position."""
    return position_interface.close_position(portfolio_id, ticker, price)


@mcp.tool()
async def pm_check_signals(portfolio_id: str, ticker: str) -> dict[str, Any]:
    """Check for add/reduce signals for an existing position."""
    return position_interface.get_trading_signals(portfolio_id, ticker)


@mcp.tool()
async def pm_get_buying_advice(
    portfolio_id: str,
    ticker: str,
    initial_risk_per_trade: float = None,
    mode: str = "ATR",
) -> dict[str, Any]:
    """Get position sizing advice for a potential new trade."""
    return position_interface.get_buying_advice(
        portfolio_id, ticker, initial_risk_per_trade, mode
    )


# ---------------------------------------------------------------------------
# Skill prompts
# ---------------------------------------------------------------------------
def _load_skill_content(skill_name: str) -> str:
    path = project_path("skills", skill_name, "SKILL.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"Skill {skill_name} not found."


@mcp.prompt()
def manage_portfolio_skill() -> str:
    """Return the operational guide for managing portfolios."""
    return _load_skill_content("manage_portfolio")


@mcp.prompt()
def manage_position_skill() -> str:
    """Return the operational guide for managing positions."""
    return _load_skill_content("manage_position")


if __name__ == "__main__":
    transport = SERVER_CONFIG.get("transport", "sse")
    logger.info("Starting MCP server: %s (%s)", SERVER_CONFIG.get("name"), transport)
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="sse",
            host=SERVER_CONFIG.get("host", "0.0.0.0"),
            port=int(SERVER_CONFIG.get("port", 8080)),
        )
