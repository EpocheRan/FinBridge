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
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # 清除所有现有的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # 创建文件处理器（每天轮转，保留7天）
    log_file = os.path.join(log_dir, 'mcp_server.log')
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    
    # 创建控制台处理器（只显示错误）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.CRITICAL)
    
    # 设置格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # 添加处理器
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    return log_dir

# 设置文件日志
log_dir = setup_file_logging()
logger = logging.getLogger('MCPServer')
logger.info("MCP服务器启动中...")

# 静默特定模块的日志
logging.getLogger('Data Loader').setLevel(logging.WARNING)
logging.getLogger('Trading Date').setLevel(logging.WARNING)
logging.getLogger('Data Fetcher').setLevel(logging.WARNING)
logging.getLogger('Data Connection').setLevel(logging.WARNING)

# 初始化数据加载器
try:
    logger.info("初始化数据加载器...")
    data_loader = DataLoader()
    trading_date_manager = TradingDateManage()
    logger.info("数据加载器初始化完成")
except Exception as e:
    logger.error(f"数据加载器初始化失败: {e}")
    raise


# 辅助函数：标准化股票代码
def normalize_code(code: str) -> str:
    """去除股票代码后缀，如 600519.SH -> 600519"""
    if isinstance(code, str):
        return code.split('.')[0]
    return str(code)

@mcp.tool()
async def get_ohlc_data(
    stock_codes: str | List[str],
    start_date: str,
    end_date: str,
    freq: str = 'd'
) -> Dict[str, Any]:
    """
    获取股票OHLCV数据
    
    Args:
        stock_codes: 股票代码，单个字符串或字符串列表
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'
        freq: 数据频率，可选值: '1min', '5min', '15min', '30min', '60min', 'd', 'w', 'm'
    
    Returns:
        包含OHLCV数据的字典，格式为 {stock_code: data_dict}
    """
    try:
        # 标准化股票代码
        if isinstance(stock_codes, str):
            stock_codes = normalize_code(stock_codes)
        elif isinstance(stock_codes, list):
            stock_codes = [normalize_code(c) for c in stock_codes]
            
        # 获取数据
        stock_data = data_loader.ohlc(stock_codes, start_date, end_date, freq)
        
        # 转换DataFrame为字典格式
        result = {}
        for stock_code, df in stock_data.items():
            if df.empty:
                result[stock_code] = {"error": "No data found", "data": []}
            else:
                # 重置索引，将时间戳转为列
                df_reset = df.reset_index()
                # 转换为字典列表
                data_list = df_reset.to_dict('records')
                # 格式化时间戳
                for record in data_list:
                    if 'ts' in record:
                        record['ts'] = record['ts'].strftime('%Y-%m-%d %H:%M:%S')
                result[stock_code] = {"data": data_list, "count": len(data_list)}
        
        return {
            "success": True,
            "message": f"Successfully retrieved OHLC data for {len(stock_codes) if isinstance(stock_codes, list) else 1} stocks",
            "result": result
        }
        
    except Exception as e:
        logger.error(f"Error in get_ohlc_data: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve OHLC data"
        }


@mcp.tool()
async def get_rolling_finance(
    stock_codes: str | List[str],
    start_date: str,
    end_date: str
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
                data_list = df_reset.to_dict('records')
                for record in data_list:
                    if 'ts' in record:
                        record['ts'] = record['ts'].strftime('%Y-%m-%d')
                result[stock_code] = {"data": data_list, "count": len(data_list)}
        
        return {
            "success": True,
            "message": f"Successfully retrieved rolling finance data for {len(stock_codes) if isinstance(stock_codes, list) else 1} stocks",
            "result": result
        }
        
    except Exception as e:
        logger.error(f"Error in get_rolling_finance: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve rolling finance data"
        }


@mcp.tool()
async def get_stock_basic_info(stock_code: Optional[str] = None) -> Dict[str, Any]:
    """
    获取股票基本信息
    
    Args:
        stock_code: 可选，股票代码。如果不提供，则返回全量信息。
        
    Returns:
        包含股票基本信息的字典
    """
    try:
        df = data_loader.stock_basic_info(stock_code)
        
        if df.empty:
            return {
                "success": False,
                "error": "No stock basic info found",
                "message": "Failed to retrieve stock basic info"
            }
        
        df_reset = df.reset_index()
        data_list = df_reset.to_dict('records')
        
        # 格式化日期字段
        for record in data_list:
            for field in ['ts', 'ipoDate', 'outDate']:
                if field in record and pd.notna(record[field]):
                    if isinstance(record[field], datetime):
                        record[field] = record[field].strftime('%Y-%m-%d')
        
        return {
            "success": True,
            "message": f"Successfully retrieved basic info for {len(data_list)} stocks",
            "result": {"data": data_list, "count": len(data_list)}
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve stock basic info"
        }


@mcp.tool()
async def get_financial_indicators(
    stock_codes: str | List[str],
    start_date: str,
    end_date: str,
    forecast: bool = False
) -> Dict[str, Any]:
    """
    获取财务指标数据
    
    Args:
        stock_codes: 股票代码，单个字符串或字符串列表
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'
        forecast: True-当季报，False-上一季报
    
    Returns:
        包含财务指标数据的字典
    """
    try:
        # 标准化股票代码
        if isinstance(stock_codes, str):
            stock_codes = normalize_code(stock_codes)
        elif isinstance(stock_codes, list):
            stock_codes = [normalize_code(c) for c in stock_codes]
            
        stock_data = data_loader.financial_indicators(stock_codes, start_date, end_date, forecast)
        
        result = {}
        for stock_code, df in stock_data.items():
            if df.empty:
                result[stock_code] = {"error": "No data found", "data": []}
            else:
                df_reset = df.reset_index()
                data_list = df_reset.to_dict('records')
                for record in data_list:
                    if 'ts' in record:
                        record['ts'] = record['ts'].strftime('%Y-%m-%d')
                result[stock_code] = {"data": data_list, "count": len(data_list)}
        
        return {
            "success": True,
            "message": f"Successfully retrieved financial indicators for {len(stock_codes) if isinstance(stock_codes, list) else 1} stocks",
            "result": result
        }
        
    except Exception as e:
        logger.error(f"Error in get_financial_indicators: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve financial indicators"
        }


@mcp.tool()
async def get_stock_industry() -> Dict[str, Any]:
    """
    获取股票行业信息
    
    Returns:
        包含行业信息的字典
    """
    try:
        df = data_loader.stock_industry()
        
        if df.empty:
            return {
                "success": False,
                "error": "No stock industry info found",
                "message": "Failed to retrieve stock industry info"
            }
        
        df_reset = df.reset_index()
        data_list = df_reset.to_dict('records')
        
        for record in data_list:
            if 'ts' in record and pd.notna(record['ts']):
                record['ts'] = record['ts'].strftime('%Y-%m-%d')
        
        return {
            "success": True,
            "message": f"Successfully retrieved industry info for {len(data_list)} stocks",
            "result": {"data": data_list, "count": len(data_list)}
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve stock industry info"
        }


@mcp.tool()
async def get_concept_boards() -> Dict[str, Any]:
    """
    获取概念板块信息
    
    Returns:
        包含概念板块信息的字典
    """
    try:
        df = data_loader.concept_boards()
        
        if df.empty:
            return {
                "success": False,
                "error": "No concept boards found",
                "message": "Failed to retrieve concept boards"
            }
        
        df_reset = df.reset_index()
        data_list = df_reset.to_dict('records')
        
        for record in data_list:
            if 'ts' in record and pd.notna(record['ts']):
                record['ts'] = record['ts'].strftime('%Y-%m-%d')
        
        return {
            "success": True,
            "message": f"Successfully retrieved {len(data_list)} concept boards",
            "result": {"data": data_list, "count": len(data_list)}
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve concept boards"
        }


@mcp.tool()
async def get_concept_boards_ohlc(
    board_names: str | List[str],
    start_date: str,
    end_date: str
) -> Dict[str, Any]:
    """
    获取概念板块OHLC数据
    
    Args:
        board_names: 板块名称，单个字符串或字符串列表
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'
    
    Returns:
        包含概念板块OHLC数据的字典
    """
    try:
        board_data = data_loader.concept_boards_ohlc(board_names, start_date, end_date)
        
        result = {}
        for board_name, df in board_data.items():
            if df.empty:
                result[board_name] = {"error": "No data found", "data": []}
            else:
                df_reset = df.reset_index()
                data_list = df_reset.to_dict('records')
                for record in data_list:
                    if 'ts' in record:
                        record['ts'] = record['ts'].strftime('%Y-%m-%d')
                result[board_name] = {"data": data_list, "count": len(data_list)}
        
        return {
            "success": True,
            "message": f"Successfully retrieved concept board OHLC data for {len(board_names)} boards",
            "result": result
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve concept board OHLC data"
        }


@mcp.tool()
async def get_concept_constituents() -> Dict[str, Any]:
    """
    获取概念板块成份股信息
    
    Returns:
        包含概念板块成份股的字典
    """
    try:
        df = data_loader.concept_constituents()
        
        if df.empty:
            return {
                "success": False,
                "error": "No concept constituents found",
                "message": "Failed to retrieve concept constituents"
            }
        
        df_reset = df.reset_index()
        data_list = df_reset.to_dict('records')
        
        for record in data_list:
            if 'ts' in record and pd.notna(record['ts']):
                record['ts'] = record['ts'].strftime('%Y-%m-%d')
        
        return {
            "success": True,
            "message": f"Successfully retrieved concept constituents for {len(data_list)} records",
            "result": {"data": data_list, "count": len(data_list)}
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve concept constituents"
        }


@mcp.tool()
async def get_trading_days(
    start_date: str,
    end_date: str
) -> Dict[str, Any]:
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
                "end_date": end_date
            }
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve trading days"
        }


@mcp.tool()
async def get_today_date() -> Dict[str, Any]:
    """
    获取今天的日期
    
    Returns:
        包含今天日期的字典，格式为 'YYYY-MM-DD'
    """
    today = datetime.now().strftime('%Y-%m-%d')
    return {
        "success": True,
        "message": "Successfully retrieved today's date",
        "result": {
            "date": today,
            "weekday": datetime.now().weekday() + 1  # 1-7
        }
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
            result = db.query('SELECT MAX(ts) FROM ohlcv_1d;', fetch_all=False)
            if result and result[0]:
                latest_ohlc = result[0].strftime('%Y-%m-%d')
        except:
            pass
        
        status_info = {
            "database_connected": is_connected,
            "latest_ohlc_date": latest_ohlc,
            "current_date": datetime.now().strftime('%Y-%m-%d'),
            "data_sources": ["questdb", "akshare", "baostock"]
        }
        
        return {
            "success": True,
            "message": "Data status retrieved successfully",
            "result": status_info
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve data status"
        }


if __name__ == "__main__":
    # 重定向标准输出和错误到文件，保持stdio干净用于MCP协议
    import sys
    import os
    
    # 创建日志目录（如果不存在）
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # 记录启动信息到文件
    stdout_log = os.path.join(log_dir, 'stdout.log')
    with open(stdout_log, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*50}\n")
        f.write(f"MCP服务器启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"服务器名称: {SERVER_NAME}\n")
        f.write(f"描述: {SERVER_DESCRIPTION}\n")
        f.write(f"{'='*50}\n")
    
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