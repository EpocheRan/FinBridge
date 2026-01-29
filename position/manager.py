import json
import os
import math
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("PositionManager")


class PositionManager:
    def __init__(
        self,
        data_file="positions.json",
        n_factor=1.5,
        time_stop_days=5,
        risk_per_trade=0.02,
    ):
        """
        Initialize the Position Manager.

        Args:
            data_file (str): Path to the JSON file storing position data.
            n_factor (float): Base volatility coefficient (default 1.5).
            time_stop_days (int): Max days to verify trend (default 5).
            risk_per_trade (float): Risk per trade as a fraction of equity (default 0.02).
        """
        self.data_file = os.path.abspath(data_file)
        self.n_factor = n_factor
        self.time_stop_days = time_stop_days
        self.risk_per_trade = risk_per_trade
        self.positions = {}
        self.state = {"total_capital": 0.0, "cash": 0.0, "positions": {}}
        self.load_data()

    def load_data(self):
        """Load position data from JSON file."""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r") as f:
                    self.state = json.load(f)
                    self.positions = self.state.get("positions", {})
                logger.info(f"已从 {self.data_file} 加载持仓数据")
            except Exception as e:
                logger.error(f"加载数据失败: {e}")
                self.positions = {}
        else:
            logger.info("未找到现有的持仓文件。开始新会话。")
            self.positions = {}

    def save_data(self):
        """Save position data to JSON file."""
        self.state["positions"] = self.positions
        try:
            with open(self.data_file, "w") as f:
                json.dump(self.state, f, indent=4)
            logger.info("持仓数据已保存。")
        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    def update_capital(self, total_capital, cash):
        """Update capital and cash info."""
        self.state["total_capital"] = total_capital
        self.state["cash"] = cash
        self.save_data()

    def calculate_base_position(self, ticker, current_price, atr, structure_stop):
        """
        2) Calculate Base Position Size.

        Args:
            ticker (str): Stock ticker.
            current_price (float): Current market price.
            atr (float): Current ATR value.
            structure_stop (float): Structural stop loss price (e.g., recent low).

        Returns:
            dict: {
                "ticker": str,
                "price": float,
                "quantity": int,
                "atr": float,
                "stop_price": float,
                "risk_distance": float,
                "reason": str
            }
        """
        if self.state["total_capital"] <= 0:
            logger.error("总资产为0或未设置。请先调用 update_capital。")
            return None

        # a. Calculate Potential Stops
        # Hard Logic: Price - 1.5 * ATR
        p_atr = current_price - (self.n_factor * atr)

        # Soft Logic: Structure Stop (Provided by user/logic)
        p_struct = structure_stop

        # b. Decision Rule: Select the ONE closer to current price (Narrower Stop)
        # Since stops are below price, the higher value is closer.
        p_stop = max(p_atr, p_struct)

        stop_type = "ATR止损" if p_stop == p_atr else "结构化止损"
        logger.info(
            f"{ticker} 止损选择: ATR止损={p_atr:.2f}, 结构化止损={p_struct:.2f}。选中: {stop_type} ({p_stop:.2f})"
        )

        # Risk Distance (D)
        risk_distance = current_price - p_stop

        if risk_distance <= 0:
            logger.error(f"无效的止损价 {p_stop} >= 当前价格 {current_price}")
            return None

        # c. Calculate Base Qty
        # Risk Capital = Total Capital * R
        risk_capital = self.state["total_capital"] * self.risk_per_trade

        # Qty based on risk
        q_risk = risk_capital / risk_distance

        # Qty based on available cash
        q_cash = self.state["cash"] / current_price

        # Final Quantity (floor to integer)
        qty = math.floor(min(q_risk, q_cash))

        if qty <= 0:
            logger.warning(f"{ticker} 计算的持仓数量为 0。请检查总资产或可用现金。")
            return None

        return {
            "ticker": ticker,
            "price": current_price,
            "quantity": qty,
            "atr": atr,
            "stop_price": p_stop,
            "risk_distance": risk_distance,
            "stop_type": stop_type,
        }

    def check_dead_position(self, ticker, current_price, current_date_str):
        """
        3) Check for 'Dead' positions to prune.

        Args:
            ticker (str): Stock ticker.
            current_price (float): Current price.
            current_date_str (str): Current date in 'YYYY-MM-DD' format.

        Returns:
            bool: True if position should be closed (Dead), False otherwise.
        """
        if ticker not in self.positions:
            return False

        pos = self.positions[ticker]
        entry_date = datetime.strptime(pos["entry_time"], "%Y-%m-%d")
        current_date = datetime.strptime(current_date_str, "%Y-%m-%d")

        # Calculate days held (trading days approx or calendar days? Spec implies trading days.
        # Assuming simple calendar days difference for prototype, or pass trading days count)
        # Here we use calendar days as a proxy or rely on caller to call this only on Nth day.
        # The prompt says: "Review when held for N trading days".
        # We will assume this check is triggered externally at the right time,
        # OR we check if (current - entry) >= N days.
        # Let's check if days_held >= time_stop_days
        days_held = (current_date - entry_date).days

        # Check triggers only if we hit the Time Limit (Time-Stop)
        # Note: The prompt says "Review AT N trading days".
        # If we are past that window and conditions weren't met, it should be closed?
        # Let's apply: If days_held >= N, run the check.

        if days_held < self.time_stop_days:
            return False  # Not yet time to judge

        # Check Checklist
        # 1. Has triggered first add-on? (Pyramid Level > 0)
        has_added = pos.get("pyramid_level", 0) > 0

        # 2. Floating Profit > 0.5 * N * ATR
        initial_atr = pos["initial_atr"]
        threshold_profit = 0.5 * self.n_factor * initial_atr
        avg_cost = pos["cost_price"]  # Average cost
        # Profit per share
        current_profit = current_price - avg_cost

        is_profitable = current_profit > threshold_profit

        logger.info(
            f"僵尸仓位检查 {ticker}: 持仓天数={days_held}, 已加仓={has_added}, 当前利润={current_profit:.2f} vs 阈值={threshold_profit:.2f}"
        )

        # Execution Action
        # "Any is Yes -> Trend Valid -> Keep"
        if has_added or is_profitable:
            return False  # Keep

        # "Both No -> Trend False -> Close"
        return True  # Dead

    def check_pyramid_signal(self, ticker, current_price):
        """
        4) Check for Pyramid Add-on signal.

        Returns:
            dict or None: If add-on triggered, returns trade details. Else None.
        """
        if ticker not in self.positions:
            return None

        pos = self.positions[ticker]

        entry_price = pos[
            "first_entry_price"
        ]  # Need to store the ORIGINAL entry price for reference grid?
        # The prompt defines triggers based on "P (Opening Price) + 0.5 D".
        # "Opening Price" usually refers to the initial entry price of the campaign.

        d = pos["initial_risk_distance"]
        level = pos["pyramid_level"]
        qty_to_buy = pos["base_quantity"]  # Equal add-on

        # Determine Trigger Price based on current level
        # Level 0 (Base) -> Next Trigger: P + 0.5D
        # Level 1 (1st Add) -> Next Trigger: P + 1.0D
        # Level 2 (2nd Add) -> Next Trigger: P + 1.5D
        # Formula: Next Trigger = First_Entry + (Level + 1) * 0.5 * D

        next_trigger_price = entry_price + (level + 1) * 0.5 * d

        if current_price >= next_trigger_price:
            # Calculate New Stop
            # Base (L0) -> L1 trigger. New Stop: P - 0.5D (Original Stop + 0.5D)
            # L1 -> L2 trigger. New Stop: P (Original Stop + 1.0D)
            # L2 -> L3 trigger. New Stop: P + 0.5D (Original Stop + 1.5D)
            # General: New Stop = Original Stop + (Level + 1) * 0.5 * D
            # Wait, let's look at the "Subsequent" rule: "Current Price - 1.0 D"

            new_stop = 0.0

            # Map levels to stop logic per table
            # Triggering Level 1 Add (moving from L0 to L1)
            if level == 0:
                # Target Stop: P - 0.5D
                new_stop = entry_price - 0.5 * d
            # Triggering Level 2 Add (moving from L1 to L2)
            elif level == 1:
                # Target Stop: P (Breakeven)
                new_stop = entry_price
            # Triggering Level 3 Add (moving from L2 to L3)
            elif level == 2:
                # Target Stop: P + 0.5D
                new_stop = entry_price + 0.5 * d
            # Subsequent (Level > 2)
            else:
                # Rule: Current Price - 1.0 D
                # Note: "Current Price" here likely means the TRIGGER price or actual market price?
                # Usually systematic logic uses the Trigger Price to define the structure.
                new_stop = current_price - 1.0 * d

            # Iron Rule: Stop only moves UP
            current_stop = pos["stop_price"]
            final_stop = max(new_stop, current_stop)

            return {
                "ticker": ticker,
                "is_pyramid": True,
                "price": current_price,
                "quantity": qty_to_buy,
                "new_stop_price": final_stop,
                "atr": pos[
                    "initial_atr"
                ],  # Carry over initial ATR or use current? Usually initial for grid.
                "level_from": level,
                "level_to": level + 1,
            }

        return None

    def record_entry(self, trade_data, date_str):
        """
        5) Record new position (Base).
        """
        ticker = trade_data["ticker"]

        # Structure for JSON
        position_entry = {
            "ticker": ticker,
            "status": "HOLDING",
            "entry_time": date_str,
            "last_trade_time": date_str,
            "first_entry_price": trade_data["price"],
            "cost_price": trade_data["price"],  # Weighted Average
            "quantity": trade_data["quantity"],
            "base_quantity": trade_data["quantity"],  # Remember for equal add-ons
            "initial_atr": trade_data["atr"],
            "stop_price": trade_data["stop_price"],
            "initial_risk_distance": trade_data["risk_distance"],
            "pyramid_level": 0,
        }

        self.positions[ticker] = position_entry

        # Deduct Cash (Simplified)
        cost = trade_data["price"] * trade_data["quantity"]
        self.state["cash"] -= cost

        self.save_data()
        logger.info(f"已记录 {ticker} 的初始建仓")

    def record_addon(self, trade_data, date_str):
        """
        Record Pyramid Add-on.
        """
        ticker = trade_data["ticker"]
        if ticker not in self.positions:
            return

        pos = self.positions[ticker]

        old_qty = pos["quantity"]
        old_cost = pos["cost_price"] * old_qty

        new_qty = trade_data["quantity"]
        new_cost = trade_data["price"] * new_qty

        total_qty = old_qty + new_qty
        avg_price = (old_cost + new_cost) / total_qty

        # Update Fields
        pos["quantity"] = total_qty
        pos["cost_price"] = avg_price
        pos["last_trade_time"] = date_str
        pos["stop_price"] = trade_data["new_stop_price"]
        pos["pyramid_level"] = trade_data["level_to"]

        # Deduct Cash
        self.state["cash"] -= new_cost

        self.save_data()
        logger.info(f"已记录 {ticker} 的加仓交易，当前层级: {pos['pyramid_level']}")

    def close_position(self, ticker, price, date_str, reason="Signal"):
        """Close position."""
        if ticker in self.positions:
            pos = self.positions[ticker]
            qty = pos["quantity"]

            # Add Cash
            proceeds = qty * price
            self.state["cash"] += proceeds

            logger.info(f"已平仓 {ticker}，平仓价: {price}。原因: {reason}")
            del self.positions[ticker]
            self.save_data()
