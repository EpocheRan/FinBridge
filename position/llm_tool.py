import logging
import json
from datetime import datetime, timedelta
from .manager import PositionManager

logger = logging.getLogger("PositionLLMTool")


class PositionAgentInterface:
    """
    Interface for Large Language Models to interact with the Position Management System.

    This class wraps the low-level PositionManager and provides a simplified,
    context-rich API suitable for AI agents.
    """

    def __init__(self, data_loader=None):
        self.manager = PositionManager(data_loader=data_loader)

    def _response(self, success: bool, message: str, data: dict = None):
        """Standardized response format for LLM reasoning."""
        return {"success": success, "message": message, "data": data or {}}

    def create_portfolio(
        self,
        name: str,
        initial_cash: float,
        reuse_existing: bool = None,
        tickers: list = None,
    ):
        """
        Create a new investment portfolio, or confirm reuse of a similar existing one.

        Args:
            name: Portfolio name.
            initial_cash: Initial capital.
            reuse_existing: True=复用相似组合；False=强制新建；None=自动探测并请求确认。
            tickers: 该组合计划交易的标的列表（用于「同标的」相似度检测）。
        """
        try:
            portfolios = self.manager.list__profolio()
            name_norm = (name or "").strip().lower()
            tickers_set = set()
            if tickers:
                tickers_set = {str(t).split(".")[0] for t in tickers}

            # 找出相似组合（同名 / 同标的）
            candidates = []
            for p in portfolios:
                reasons = []
                if p.name and p.name.strip().lower() == name_norm:
                    reasons.append("same_name")
                if tickers_set:
                    held = {
                        pos.ticker
                        for pos in self.manager.list_positions(p.portfolio_id)
                        if pos.ticker
                    }
                    if held & tickers_set:
                        reasons.append("same_ticker")
                if reasons:
                    candidates.append(
                        {
                            "portfolio_id": p.portfolio_id,
                            "name": p.name,
                            "cash": p.cash,
                            "total_assets": p.total_assets,
                            "reason": reasons,
                        }
                    )

            # 未指定 reuse_existing 且存在相似组合 -> 请求确认（不创建）
            if candidates and reuse_existing is None:
                first = candidates[0]
                reasons_cn = "/".join(
                    "同名" if r == "same_name" else "同标的" for r in first["reason"]
                )
                return self._response(
                    False,
                    f"发现相似组合「{first['name']}」({first['portfolio_id']})，原因：{reasons_cn}。"
                    f"请确认：复用现有组合请再次调用并传 reuse_existing=true；"
                    f"要新建则传 reuse_existing=false。",
                    {"needs_confirmation": True, "candidates": candidates},
                )

            # 决定复用
            if candidates and reuse_existing is True:
                first = candidates[0]
                return self._response(
                    True,
                    f"复用现有组合「{first['name']}」({first['portfolio_id']})。",
                    {
                        "portfolio_id": first["portfolio_id"],
                        "is_new": False,
                        "reused": True,
                        "candidates": candidates,
                    },
                )

            # 新建（reuse_existing=False 或 无相似组合）
            pid = self.manager.create_profolio(
                name, initial_cash, force=(reuse_existing is False)
            )
            if pid:
                return self._response(
                    True,
                    f"Portfolio '{name}' created successfully.",
                    {"portfolio_id": pid, "is_new": True},
                )
            return self._response(False, "Failed to create portfolio. Check logs.")
        except Exception as e:
            return self._response(False, f"Error creating portfolio: {str(e)}")

    def list_portfolios(self):
        """
        List all available investment portfolios.
        """
        try:
            portfolios = self.manager.list__profolio()
            data = []
            for p in portfolios:
                data.append(
                    {
                        "portfolio_id": p.portfolio_id,
                        "name": p.name,
                        "cash": p.cash,
                        "market_value": p.market_value,
                        "total_assets": p.total_assets,
                    }
                )
            return self._response(
                True, f"Found {len(data)} portfolios.", {"portfolios": data}
            )
        except Exception as e:
            return self._response(False, f"Error listing portfolios: {str(e)}")

    def delete_portfolio(self, portfolio_id: str):
        """
        Delete a portfolio and all its associated data (positions, trades).
        """
        try:
            success = self.manager.delete_profolio(portfolio_id)
            if success:
                return self._response(True, f"Portfolio {portfolio_id} deleted.")
            return self._response(False, f"Failed to delete portfolio {portfolio_id}.")
        except Exception as e:
            return self._response(False, f"Error deleting portfolio: {str(e)}")

    def modify_portfolio(self, portfolio_id: str, name: str = None, cash: float = None):
        """
        Modify portfolio name or cash balance.

        Args:
            portfolio_id: Portfolio ID.
            name: New name (optional).
            cash: New cash balance (optional).
        """
        try:
            updates = {}
            if name is not None:
                updates["name"] = name
            if cash is not None:
                updates["cash"] = cash

            if not updates:
                return self._response(False, "No updates provided.")

            success = self.manager.modify_profolio(portfolio_id, updates)
            if success:
                return self._response(
                    True, f"Portfolio {portfolio_id} updated.", updates
                )
            return self._response(False, f"Failed to update portfolio {portfolio_id}.")
        except Exception as e:
            return self._response(False, f"Error updating portfolio: {str(e)}")

    def modify_position(
        self,
        pos_id: str,
        name: str = None,
        quantity: float = None,
        mode_params: dict = None,
    ):
        """
        Modify position name, quantity, or mode parameters.

        Args:
            pos_id: Position ID (can be 8-char short ID like 'abc12345' or full UUID).
            name: New name (optional).
            quantity: New quantity (optional).
            mode_params: 要更新的模式参数（会与现有参数合并），
                         例如 ATR: {"n_factor": 2.5, "risk_per_trade": 0.03}.
        """
        try:
            # 规范化 pos_id
            short_id = pos_id
            if len(pos_id) > 8 and "-" in pos_id:
                # 完整 UUID 格式，尝试通过模糊匹配查找
                # 使用前缀匹配
                from utils.DataConnect import questDB
                from sqlalchemy import text

                db = questDB()
                search_pattern = pos_id[:8] + "%"
                with db.pd_conn.connect() as conn:
                    result = conn.execute(
                        text(
                            "SELECT pos_id FROM positions WHERE pos_id LIKE :pattern AND deleted_at IS NULL LIMIT 1"
                        ),
                        {"pattern": search_pattern},
                    ).fetchone()

                if result:
                    short_id = result[0]
                    logger.info(f"Found position by UUID prefix: {short_id}")
                else:
                    return self._response(
                        False, f"Position not found for UUID: {pos_id}"
                    )

            updates = {}
            if name is not None:
                updates["name"] = name
            if quantity is not None:
                updates["quantity"] = quantity
            if mode_params:
                updates["mode_params"] = mode_params

            if not updates:
                return self._response(False, "No updates provided.")

            success = self.manager.modify_position(short_id, updates)
            if success:
                return self._response(True, f"Position {short_id} updated.", updates)
            return self._response(False, f"Failed to update position {short_id}.")
        except Exception as e:
            return self._response(False, f"Error updating position: {str(e)}")

    def get_portfolio_status(self, portfolio_id: str):
        """
        Get the current status of the portfolio, including all positions and PnL.

        Args:
            portfolio_id: The ID of the portfolio to query.
        """
        try:
            summary = self.manager.get_portfolio_summary(portfolio_id)
            if "error" in summary:
                return self._response(False, summary["error"])

            return self._response(True, "Portfolio status retrieved.", summary)
        except Exception as e:
            return self._response(False, f"Error retrieving status: {str(e)}")

    def get_available_modes(self):
        """
        List all available risk management and position sizing modes.
        """
        modes = [
            {
                "name": "ATR",
                "description": "基于平均真实波幅 (ATR) 的动态风控模式。包含追踪止损、加仓信号检测以及基于风险百分比的头寸计算。",
                "parameters": {
                    "initial_atr": "初始 ATR 值（可选，若不提供则自动从历史数据计算）",
                    "n_factor": "ATR 系数，通常为 2.0，用于确定止损距离",
                    "risk_per_trade": "单笔交易风险比例，例如 0.02 表示账户资金的 2%",
                    "time_stop_days": "僵尸仓位自动清理天数",
                },
            },
            {
                "name": "GRID",
                "description": "网格交易模式：以中枢价为核心上下铺设网格，下跌逐格买入、上涨逐格卖出，跌破下界清仓，适合震荡行情。",
                "parameters": {
                    "base_price": "网格中枢价（可选，不填则以当前价为准）",
                    "grid_step": "网格间距（绝对价格，优先使用）",
                    "grid_step_pct": "网格间距（百分比，例如 0.02 表示 2%）",
                    "grid_count": "上下各铺设几格（默认 5）",
                    "quantity_per_grid": "每格交易数量",
                    "max_position": "最大持仓数量（0 表示不限）",
                    "lower_bound": "网格下界（0 表示自动 = base - count*step）",
                    "upper_bound": "网格上界（0 表示自动 = base + count*step）",
                },
            },
        ]
        return self._response(True, "Available modes retrieved.", {"modes": modes})

    def open_position(
        self, portfolio_id: str, ticker: str, name: str, mode: str, **kwargs
    ):
        """
        Open a new tracking position for a stock (does not execute a trade, just sets up tracking).

        Args:
            portfolio_id: Portfolio ID.
            ticker: Stock ticker symbol (e.g., "AAPL", "600519").
            name: Stock name (optional).
            mode: Risk management mode (default "ATR").
            kwargs: Additional mode parameters (e.g., initial_atr, n_factor).
        """
        # Check if already exists
        existing = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if existing:
            return self._response(
                False,
                f"Position for {ticker} already exists. Use 'add_trade' to modify it.",
            )

        try:
            pos_id = self.manager.create_position(
                portfolio_id, ticker, name, mode, kwargs
            )
            if pos_id:
                return self._response(
                    True, f"Position tracking started for {ticker}.", {"pos_id": pos_id}
                )
            return self._response(False, "Failed to create position.")
        except Exception as e:
            return self._response(False, f"Error creating position: {str(e)}")

    def execute_trade(
        self, portfolio_id: str, ticker: str, action: str, quantity: float, price: float
    ):
        """
        Record a buy or sell trade for a specific ticker.

        Args:
            portfolio_id: Portfolio ID.
            ticker: Stock ticker.
            action: "Buy" (买入) or "Sell" (卖出).
            quantity: Number of shares.
            price: Transaction price.
        """
        # 1. Resolve Position ID
        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            # Auto-create position if buying? Maybe better to force explicit creation for clarity.
            # But for LLM convenience, let's auto-create if it's a Buy.
            if action.lower() in ["buy", "买入"]:
                self.open_position(portfolio_id, ticker, ticker, "ATR")
                position = self.manager.get_position_by_ticker(portfolio_id, ticker)
                if not position:
                    return self._response(
                        False, f"Could not create position for {ticker}."
                    )
            else:
                return self._response(
                    False, f"Cannot sell {ticker}: Position not found."
                )

        # 2. Normalize Action
        trade_type = "买入" if action.lower() in ["buy", "买入"] else "卖出"

        # 3. Check Cash & Prepare Update
        amount = quantity * price
        summary = self.manager.get_portfolio_summary(portfolio_id)
        if "error" in summary:
            return self._response(False, summary["error"])
        current_cash = summary["cash"]

        if trade_type == "买入":
            if current_cash < amount:
                return self._response(
                    False,
                    f"Insufficient cash. Available: {current_cash}, Required: {amount}",
                )
            cash_change = -amount
        else:
            cash_change = amount

        # 4. Execute
        try:
            trade_id = self.manager.add_trade_record(
                position.pos_id, trade_type, quantity, price
            )
            if trade_id:
                # Update Cash
                new_cash = current_cash + cash_change
                self.manager.modify_profolio(portfolio_id, {"cash": new_cash})

                # Refresh stats
                # calculate_profit is called inside add_trade_record
                self.manager.calculate_profolio(portfolio_id)

                updated_pos = self.manager.get_position_by_ticker(portfolio_id, ticker)

                return self._response(
                    True,
                    f"Trade recorded: {action} {quantity} {ticker} @ {price}.",
                    {
                        "trade_id": trade_id,
                        "new_quantity": updated_pos.quantity,
                        "new_cost": updated_pos.cost,
                        "unrealized_pnl": updated_pos.unrealized_profit,
                        "cash_balance": new_cash,
                    },
                )
            return self._response(False, "Failed to record trade.")
        except Exception as e:
            return self._response(False, f"Error recording trade: {str(e)}")

    def close_position(self, portfolio_id: str, ticker: str, price: float):
        """
        Fully close a position (Sell all).

        Args:
            portfolio_id: Portfolio ID.
            ticker: Stock ticker.
            price: Selling price.
        """
        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            return self._response(False, f"Position {ticker} not found.")

        if position.quantity <= 0:
            return self._response(False, f"Position {ticker} has 0 quantity.")

        return self.execute_trade(
            portfolio_id, ticker, "Sell", position.quantity, price
        )

    def get_trading_signals(self, portfolio_id: str, ticker: str):
        """
        Check for add (pyramid) or reduce (stop loss/take profit) signals for an EXISTING position.

        Args:
            portfolio_id: Portfolio ID.
            ticker: Stock ticker.
        """
        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            return self._response(False, f"Position {ticker} not found.")

        # Get current price
        try:
            # We need OHLCV to calculate ATR if needed, but signals mostly need current price
            # and maybe ATR if it's dynamic.
            # Let's get recent data.
            end_str = datetime.now().strftime("%Y-%m-%d")
            start_str = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

            if self.manager.data_loader:
                data_map = self.manager.data_loader.ohlc(
                    ticker, start_str, end_str, freq="d", now=True
                )
                ohlcv = data_map.get(ticker)
            else:
                ohlcv = None

            if ohlcv is None or ohlcv.empty:
                return self._response(False, f"No market data for {ticker}.")

            current_price = float(ohlcv["close"].iloc[-1])

            # Prepare position_info for mode
            if not hasattr(position, "mode_instance") or not position.mode_instance:
                position.mode_instance = self.manager._get_mode_instance(
                    position.mode, position.mode_params
                )

            mode = position.mode_instance

            # Construct info dict
            info = {
                "current_price": current_price,
                "ohlcv": ohlcv,
                "days_held": (datetime.now() - position.created_at).days,
                "first_entry_price": position.cost,
                "avg_cost": position.cost,
                "cost_price": position.cost,
                "quantity": position.quantity,
                "pyramid_level": 0,  # Assumption: 0 if not tracked elsewhere
                "has_added": False,  # Assumption
            }

            # Check signals
            add_sig = mode.check_add_signal(info)
            reduce_sig = mode.check_reduce_signal(info)

            return self._response(
                True,
                "Signals checked.",
                {
                    "ticker": ticker,
                    "current_price": current_price,
                    "add_signal": add_sig,
                    "reduce_signal": reduce_sig,
                    "stop_price": mode.stop_price,
                },
            )

        except Exception as e:
            return self._response(False, f"Error checking signals: {str(e)}")

    def get_buying_advice(
        self,
        portfolio_id: str,
        ticker: str,
        initial_risk_per_trade: float = None,
        mode: str = "ATR",
    ):
        """
        Calculate suggested position size for a NEW position based on risk management.

        Args:
            portfolio_id: Portfolio ID.
            ticker: Stock Ticker.
            initial_risk_per_trade: Optional override for risk % (e.g. 0.02).
        """
        # Get Portfolio for capital
        summary = self.manager.get_portfolio_summary(portfolio_id)
        if "error" in summary:
            return self._response(False, summary["error"])

        total_capital = summary.get("total_assets", 0)
        cash = summary.get("cash", 0)

        # Get Data
        try:
            end_str = datetime.now().strftime("%Y-%m-%d")
            start_str = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

            if self.manager.data_loader:
                data_map = self.manager.data_loader.ohlc(
                    ticker, start_str, end_str, freq="d", now=True
                )
                ohlcv = data_map.get(ticker)
            else:
                ohlcv = None

            if ohlcv is None or ohlcv.empty:
                return self._response(False, f"No market data for {ticker}.")

            # 按指定模式实例化（走注册表工厂，支持 ATR / GRID 等）
            from .modes import create_mode

            mode_instance = create_mode(
                mode, {"risk_per_trade": initial_risk_per_trade or 0.02}
            )

            advice = mode_instance.calculate_purchase_advice(
                {
                    "ohlcv": ohlcv,
                    "total_capital": total_capital,
                    "available_cash": cash,
                }
            )

            return self._response(True, "Buying advice calculated.", advice)

        except Exception as e:
            return self._response(False, f"Error calculating advice: {str(e)}")
