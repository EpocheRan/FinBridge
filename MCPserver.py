"""FinBridge MCP Server。

职责边界：
- 只负责组装 MCP 工具与启动服务。
- 行情/财务逻辑在 ``data_interface``。
- 持仓逻辑在 ``position``。
- 不在启动时检查或创建数据库表；表由 ``scripts/init_database.py``、
  存量库升级由 ``scripts/migrate_database.py`` 完成。

所有服务通过 ``data_interface.api.runtime`` 取进程内单例，避免同一进程里
出现多套 QuestDB 连接与多套 Tushare 限流器。
"""

from __future__ import annotations

import logging
import signal
from logging.handlers import RotatingFileHandler
from typing import Any

from fastmcp import FastMCP

from data_interface.api.concept_tools import register_concept_tools
from data_interface.api.financial_tools import register_financial_tools
from data_interface.api.market_tools import register_market_tools
from data_interface.api.moneyflow_tools import register_moneyflow_tools
from data_interface.api.runtime import get_position_interface
from data_interface.common.config import get_config, project_path
from scheduled_tasks.api import register_scheduled_tools
from screener.interface.api import register_screener_tools

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
    console.setLevel(getattr(logging, LOGGING_CONFIG.get("console_level", "ERROR").upper(), logging.ERROR))
    console.setFormatter(formatter)
    root.addHandler(console)

    for noisy in ("DataInterface", "PositionManager", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger("MCPServer")

mcp = FastMCP(name=SERVER_CONFIG.get("name", "QuantDataServer"))

register_market_tools(mcp)
register_financial_tools(mcp)
register_moneyflow_tools(mcp)
register_concept_tools(mcp)
register_scheduled_tools(mcp)
register_screener_tools(mcp)


# ---------------------------------------------------------------------------
# 持仓工具（保留原有 Agent 交互契约）
# ---------------------------------------------------------------------------
def _pm():
    return get_position_interface()


@mcp.tool()
def pm_create_portfolio(
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
    return _pm().create_portfolio(name, initial_cash, reuse_existing, tickers)


@mcp.tool()
def pm_list_portfolios() -> dict[str, Any]:
    """List all available investment portfolios."""
    return _pm().list_portfolios()


@mcp.tool()
def pm_delete_portfolio(portfolio_id: str) -> dict[str, Any]:
    """Delete a portfolio and all its data."""
    return _pm().delete_portfolio(portfolio_id)


@mcp.tool()
def pm_modify_portfolio(
    portfolio_id: str, name: str = None, cash: float = None
) -> dict[str, Any]:
    """Modify portfolio name or cash balance."""
    return _pm().modify_portfolio(portfolio_id, name, cash)


@mcp.tool()
def pm_modify_position(
    pos_id: str,
    name: str = None,
    quantity: float = None,
    n_factor: float = None,
    risk_per_trade: float = None,
    mode_params: dict = None,
) -> dict[str, Any]:
    """Modify position name, quantity, or risk parameters.

    ``quantity`` 会以「调整流水」的形式落库（不是直接改字段），
    因此后续盈亏重算不会把手改的数量抹掉。
    """
    merged = dict(mode_params or {})
    if n_factor is not None:
        merged["n_factor"] = n_factor
    if risk_per_trade is not None:
        merged["risk_per_trade"] = risk_per_trade
    return _pm().modify_position(pos_id, name, quantity, merged or None)


@mcp.tool()
def pm_get_portfolio_status(portfolio_id: str) -> dict[str, Any]:
    """Get the current status of the portfolio."""
    return _pm().get_portfolio_status(portfolio_id)


@mcp.tool()
def pm_list_modes() -> dict[str, Any]:
    """List all available risk management and position sizing modes."""
    return _pm().get_available_modes()


@mcp.tool()
def pm_open_position(
    portfolio_id: str,
    ticker: str,
    name: str = None,
    mode: str = "ATR",
    initial_atr: float = None,
    n_factor: float = None,
    risk_per_trade: float = None,
) -> dict[str, Any]:
    """Open a new tracking position (name 缺省时用 ticker)。"""
    kwargs = {}
    if initial_atr is not None:
        kwargs["initial_atr"] = initial_atr
    if n_factor is not None:
        kwargs["n_factor"] = n_factor
    if risk_per_trade is not None:
        kwargs["risk_per_trade"] = risk_per_trade
    return _pm().open_position(portfolio_id, ticker, name, mode, **kwargs)


@mcp.tool()
def pm_execute_trade(
    portfolio_id: str, ticker: str, action: str, quantity: float, price: float
) -> dict[str, Any]:
    """Record a buy or sell trade.

    ``action`` 只接受 buy/买入、sell/卖出/close；其它值会返回错误而不是
    被当成卖出。卖出数量不能超过当前持仓。
    """
    return _pm().execute_trade(portfolio_id, ticker, action, quantity, price)


@mcp.tool()
def pm_close_position(
    portfolio_id: str, ticker: str, price: float
) -> dict[str, Any]:
    """Fully close a position at the given price."""
    return _pm().close_position(portfolio_id, ticker, price)


@mcp.tool()
def pm_check_signals(portfolio_id: str, ticker: str) -> dict[str, Any]:
    """Check for add/reduce signals for an existing position."""
    return _pm().get_trading_signals(portfolio_id, ticker)


@mcp.tool()
def pm_get_buying_advice(
    portfolio_id: str,
    ticker: str,
    initial_risk_per_trade: float = None,
    mode: str = "ATR",
) -> dict[str, Any]:
    """Get position sizing advice for a potential new trade."""
    return _pm().get_buying_advice(
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


def _resolve_transport(name: str) -> str:
    """fastmcp 2.x/3.x 的传输名兼容：3.x 推荐 http，2.x 用 sse。"""
    key = (name or "http").strip().lower()
    if key in ("http", "streamable-http", "streamable_http"):
        return "http"
    if key in ("sse",):
        return "sse"
    if key in ("stdio",):
        return "stdio"
    logger.warning("未知 transport=%s，回退 http", name)
    return "http"


def _install_signal_handlers() -> None:
    def _shutdown(signum, _frame):
        logger.info("收到信号 %s，开始关闭", signum)
        from data_interface.api.runtime import reset_singletons

        reset_singletons()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    transport = _resolve_transport(SERVER_CONFIG.get("transport", "http"))
    host = SERVER_CONFIG.get("host", "0.0.0.0")
    port = int(SERVER_CONFIG.get("port", 8080))
    logger.info("Starting MCP server: %s (%s %s:%s)", SERVER_CONFIG.get("name"), transport, host, port)
    _install_signal_handlers()
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=transport, host=host, port=port)
