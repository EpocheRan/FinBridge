"""
MCP Server for Quantitative Trading Data
使用FastMCP暴露DataLoader的数据获取接口
"""

import os
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastmcp import FastMCP
import pandas as pd

# 导入本地模块
from utils.DataLoader import DataLoader
from utils.TradingDateManage import TradingDateManage
from scripts.initialize import check_and_initialize_tables
from position import PositionAgentInterface


# -----------------------------------------------------------------------------
# Helper Functions for Humanized Responses
# -----------------------------------------------------------------------------


def create_response(
    success: bool,
    message: str,
    result: Any = None,
    error_type: str = None,
    suggestion: str = None,
) -> Dict[str, Any]:
    """
    构建标准化的 Agent 响应格式

    Args:
        success: 是否成功
        message: 给 Agent 看的自然语言描述
        result: 实际数据 payload
        error_type: 错误类型标识 (如 'InsufficientFunds', 'DataNotFound')
        suggestion: 给 Agent 的下一步行动建议 (关键!)
    """
    response = {
        "success": success,
        "message": message,
    }

    if result is not None:
        response["result"] = result

    if not success:
        # 当失败时，提供详细的指导
        response["error_details"] = {
            "type": error_type or "UnknownError",
            "suggestion": suggestion
            or "请检查输入参数格式是否正确，或尝试简化查询条件。",
        }

    return response


# 直接定义服务器配置（stdio方式不需要配置文件）
SERVER_NAME = "QuantDataServer"
SERVER_DESCRIPTION = "量化交易数据获取服务"

# 创建MCP服务器实例
mcp = FastMCP(name=SERVER_NAME)

# 配置日志保存到文件
import logging
from logging.handlers import RotatingFileHandler
import os


def setup_file_logging():
    """设置文件日志"""
    # 创建日志目录
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 清除所有现有的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 创建文件处理器（每天轮转，保留7天）
    log_file = os.path.join(log_dir, "mcp_server.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)

    # 创建控制台处理器（只显示错误）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.CRITICAL)

    # 设置格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 添加处理器
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return log_dir


# 设置文件日志
log_dir = setup_file_logging()
logger = logging.getLogger("MCPServer")
logger.info("MCP服务器启动中...")

# 静默特定模块的日志
logging.getLogger("Data Loader").setLevel(logging.WARNING)
logging.getLogger("Trading Date").setLevel(logging.WARNING)
logging.getLogger("Data Fetcher").setLevel(logging.WARNING)
logging.getLogger("Data Connection").setLevel(logging.WARNING)

# 初始化数据加载器
try:
    logger.info("检查并初始化数据库表...")
    check_and_initialize_tables()
    logger.info("数据库表检查完成")

    logger.info("初始化数据加载器...")
    data_loader = DataLoader()
    trading_date_manager = TradingDateManage()
    position_interface = PositionAgentInterface(data_loader=data_loader)
    logger.info("数据加载器初始化完成")


except Exception as e:
    logger.error(f"数据加载器初始化失败: {e}")
    raise


# 辅助函数：标准化股票代码
def normalize_code(code: str) -> str:
    """去除股票代码后缀，如 600519.SH -> 600519"""
    if isinstance(code, str):
        return code.split(".")[0]
    return str(code)


@mcp.tool()
async def get_ohlc_data(
    stock_codes: str | List[str],
    start_date: str,
    end_date: str,
    freq: str = "d",
    now: bool = False,
) -> Dict[str, Any]:
    """
    获取股票OHLCV数据

    Args:
        stock_codes: 股票代码，单个字符串或字符串列表
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'
        freq: 数据频率，可选值: '1min', '5min', '15min', '30min', '60min', 'd', 'w', 'm'
        now: 是否获取实时行情并拼接到最新位置

    Returns:
        包含OHLCV数据的字典，格式为 {stock_code: data_dict}
    """
    # 1. 基础参数校验
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        if start_dt > end_dt:
            return create_response(
                success=False,
                message=f"开始日期 ({start_date}) 不能晚于结束日期 ({end_date})",
                error_type="InvalidDateRange",
                suggestion="请交换开始日期和结束日期的顺序。",
            )
    except ValueError:
        return create_response(
            success=False,
            message=f"日期格式错误",
            error_type="DateFormatError",
            suggestion="请确保日期格式严格为 'YYYY-MM-DD' (例如 '2023-01-01')。",
        )

    try:
        # 标准化股票代码
        if isinstance(stock_codes, str):
            stock_codes = normalize_code(stock_codes)
        elif isinstance(stock_codes, list):
            stock_codes = [normalize_code(c) for c in stock_codes]

        # 检查是否为空
        if not stock_codes:
            return create_response(
                success=False,
                message="股票代码列表为空",
                error_type="EmptyInput",
                suggestion="请提供至少一个有效的股票代码。",
            )

        # 获取数据
        stock_data = data_loader.ohlc(stock_codes, start_date, end_date, freq, now=now)

        # 转换DataFrame为字典格式
        result = {}
        valid_count = 0

        for stock_code, df in stock_data.items():
            if df.empty:
                result[stock_code] = {"status": "no_data", "data": []}
            else:
                valid_count += 1
                # 重置索引，将时间戳转为列
                df_reset = df.reset_index()
                # 转换为字典列表
                data_list = df_reset.to_dict("records")
                # 格式化时间戳
                for record in data_list:
                    if "ts" in record:
                        record["ts"] = record["ts"].strftime("%Y-%m-%d %H:%M:%S")
                result[stock_code] = {"data": data_list, "count": len(data_list)}

        if valid_count == 0:
            return create_response(
                success=False,
                message=f"未找到所查询股票 ({stock_codes}) 在该时间段的数据。",
                error_type="NoMarketData",
                suggestion=f"1. 请确认当前是否为非交易日。\n2. 请确认股票代码是否存在 (当前尝试: {stock_codes})。\n3. 请尝试扩大日期范围。",
            )

        return create_response(
            success=True,
            message=f"成功获取了 {valid_count} 只股票的行情数据。",
            result=result,
        )

    except Exception as e:
        logger.error(f"Error in get_ohlc_data: {e}")
        return create_response(
            success=False,
            message="获取行情数据时发生系统异常",
            error_type="SystemError",
            suggestion=f"这是一个底层数据接口错误。错误信息: {str(e)}。请稍后重试或联系管理员。",
        )


@mcp.tool()
async def get_rolling_finance(
    stock_codes: str | List[str], start_date: str, end_date: str
) -> Dict[str, Any]:
    """
    获取滚动财务指标数据

    Args:
        stock_codes: 股票代码，单个字符串或字符串列表
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'

    Returns:
        包含财务指标数据的字典
    """
    try:
        # 标准化股票代码
        if isinstance(stock_codes, str):
            stock_codes = normalize_code(stock_codes)
        elif isinstance(stock_codes, list):
            stock_codes = [normalize_code(c) for c in stock_codes]

        stock_data = data_loader.rolling_finance(stock_codes, start_date, end_date)

        result = {}
        for stock_code, df in stock_data.items():
            if df.empty:
                result[stock_code] = {"error": "No data found", "data": []}
            else:
                df_reset = df.reset_index()
                data_list = df_reset.to_dict("records")
                for record in data_list:
                    if "ts" in record:
                        record["ts"] = record["ts"].strftime("%Y-%m-%d")
                result[stock_code] = {"data": data_list, "count": len(data_list)}

        return {
            "success": True,
            "message": f"Successfully retrieved rolling finance data for {len(stock_codes) if isinstance(stock_codes, list) else 1} stocks",
            "result": result,
        }

    except Exception as e:
        logger.error(f"Error in get_rolling_finance: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve rolling finance data",
        }


@mcp.tool()
async def get_ticker_basic_info(stock_code: str) -> Dict[str, Any]:
    """
    获取股票/基金基本信息

    Args:
        stock_code: 股票/基金代码（必填）。
    """
    try:
        # 标准化代码
        norm_code = normalize_code(stock_code)

        df = data_loader.ticker_basic_info(norm_code)

        if df.empty:
            return create_response(
                success=False,
                message=f"未找到代码 {stock_code} 的基本信息",
                error_type="NotFound",
                suggestion="请检查股票代码是否正确 (如: 600519)。暂不支持期货或指数代码。",
            )

        df_reset = df.reset_index()
        data_list = df_reset.to_dict("records")

        # 格式化日期字段
        for record in data_list:
            for field in ["ts", "ipoDate", "outDate", "list_date", "found_date"]:
                if field in record and pd.notna(record[field]):
                    if isinstance(record[field], datetime):
                        record[field] = record[field].strftime("%Y-%m-%d")

        return create_response(
            success=True,
            message=f"成功获取 {stock_code} 基本信息",
            result={"data": data_list, "count": len(data_list)},
        )

    except Exception as e:
        return create_response(
            success=False,
            message=f"获取基本信息失败",
            error_type="SystemError",
            suggestion=str(e),
        )


@mcp.tool()
async def get_trading_days(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    获取交易日列表

    Args:
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'

    Returns:
        包含交易日列表的字典
    """
    try:
        trading_days = trading_date_manager.get_trading_days(start_date, end_date)

        return {
            "success": True,
            "message": f"Successfully retrieved {len(trading_days)} trading days",
            "result": {
                "data": trading_days,
                "count": len(trading_days),
                "start_date": start_date,
                "end_date": end_date,
            },
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve trading days",
        }


@mcp.tool()
async def get_today_date() -> Dict[str, Any]:
    """
    获取今天的日期

    Returns:
        包含今天日期的字典，格式为 'YYYY-MM-DD'
    """
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "success": True,
        "message": "Successfully retrieved today's date",
        "result": {
            "date": today,
            "weekday": datetime.now().weekday() + 1,  # 1-7
        },
    }


@mcp.tool()
async def get_data_status() -> Dict[str, Any]:
    """
    获取数据源状态信息

    Returns:
        包含数据源状态的字典
    """
    try:
        from utils.DataConnect import questDB

        # 检查数据库连接状态
        db = questDB()
        is_connected = db.is_connected()

        # 获取最新数据日期
        latest_ohlc = None
        try:
            result = db.query("SELECT MAX(ts) FROM ohlcv_1d;", fetch_all=False)
            if result and result[0]:
                latest_ohlc = result[0].strftime("%Y-%m-%d")
        except:
            pass

        status_info = {
            "database_connected": is_connected,
            "latest_ohlc_date": latest_ohlc,
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "data_sources": ["questdb", "akshare", "baostock"],
        }

        return {
            "success": True,
            "message": "Data status retrieved successfully",
            "result": status_info,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve data status",
        }


# --- Position Management Tools ---


@mcp.tool()
async def pm_create_portfolio(name: str, initial_cash: float) -> Dict[str, Any]:
    """Create a new investment portfolio."""
    return position_interface.create_portfolio(name, initial_cash)


@mcp.tool()
async def pm_list_portfolios() -> Dict[str, Any]:
    """List all available investment portfolios."""
    return position_interface.list_portfolios()


@mcp.tool()
async def pm_delete_portfolio(portfolio_id: str) -> Dict[str, Any]:
    """Delete a portfolio and all its data."""
    return position_interface.delete_portfolio(portfolio_id)


@mcp.tool()
async def pm_modify_portfolio(
    portfolio_id: str, name: str = None, cash: float = None
) -> Dict[str, Any]:
    """
    Modify portfolio name or cash balance.

    Args:
        portfolio_id: Portfolio ID to modify.
        name: New portfolio name (optional).
        cash: New cash balance (optional).
    """
    return position_interface.modify_portfolio(portfolio_id, name, cash)


@mcp.tool()
async def pm_modify_position(
    pos_id: str, name: str = None, quantity: float = None
) -> Dict[str, Any]:
    """
    Modify position name or quantity.

    Args:
        pos_id: Position ID to modify.
        name: New position name/stock name (optional).
        quantity: New quantity (optional).
    """
    return position_interface.modify_position(pos_id, name, quantity)


@mcp.tool()
async def pm_get_portfolio_status(portfolio_id: str) -> Dict[str, Any]:
    """Get the current status of the portfolio."""
    return position_interface.get_portfolio_status(portfolio_id)


@mcp.tool()
async def pm_list_modes() -> Dict[str, Any]:
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
) -> Dict[str, Any]:
    """
    Open a new tracking position.
    Note: You should call pm_list_modes first to let user choose an appropriate mode.
    """
    kwargs = {}
    if initial_atr is not None:
        kwargs["initial_atr"] = initial_atr
    if n_factor is not None:
        kwargs["n_factor"] = n_factor
    return position_interface.open_position(portfolio_id, ticker, name, mode, **kwargs)


@mcp.tool()
async def pm_execute_trade(
    portfolio_id: str, ticker: str, action: str, quantity: float, price: float
) -> Dict[str, Any]:
    """Record a buy or sell trade."""
    return position_interface.execute_trade(
        portfolio_id, ticker, action, quantity, price
    )


@mcp.tool()
async def pm_close_position(
    portfolio_id: str, ticker: str, price: float
) -> Dict[str, Any]:
    """Fully close a position."""
    return position_interface.close_position(portfolio_id, ticker, price)


@mcp.tool()
async def pm_check_signals(portfolio_id: str, ticker: str) -> Dict[str, Any]:
    """Check for add/reduce signals for an existing position."""
    return position_interface.get_trading_signals(portfolio_id, ticker)


@mcp.tool()
async def pm_get_buying_advice(
    portfolio_id: str, ticker: str, initial_risk_per_trade: float = None
) -> Dict[str, Any]:
    """Get position sizing advice for a potential new trade."""
    return position_interface.get_buying_advice(
        portfolio_id, ticker, initial_risk_per_trade
    )


if __name__ == "__main__":
    # 重定向标准输出和错误到文件，保持stdio干净用于MCP协议
    import sys
    import os

    # 创建日志目录（如果不存在）
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # 记录启动信息到文件
    stdout_log = os.path.join(log_dir, "stdout.log")
    with open(stdout_log, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 50}\n")
        f.write(f"MCP服务器启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"服务器名称: {SERVER_NAME}\n")
        f.write(f"描述: {SERVER_DESCRIPTION}\n")
        f.write(f"{'=' * 50}\n")

    logger.info("启动MCP服务器...")

    # 运行MCP服务器（使用stdio方式）
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("MCP服务器被用户中断")
    except Exception as e:
        logger.error(f"MCP服务器运行异常: {e}")
        raise
    finally:
        logger.info("MCP服务器停止")
