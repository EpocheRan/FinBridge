from .base import PositionModeBase
import logging
import pandas as pd
import sys
import os

# Import directly from indicator module
from indicator import ATR, IndicatorEngine

logger = logging.getLogger("ATRMode")


class ATRMode(PositionModeBase):
    def __init__(
        self,
        stop_price=0.0,
        initial_atr=0.0,
        n_factor=2.0,
        time_stop_days=5,
        risk_per_trade=0.02,
        pyramid_level=0,
    ):
        super().__init__(stop_price)
        self.initial_atr = initial_atr
        self.n_factor = n_factor
        self.time_stop_days = time_stop_days
        self.risk_per_trade = risk_per_trade
        self.pyramid_level = pyramid_level

    def _calculate_atr_from_ohlcv(self, ohlcv: pd.DataFrame) -> float:
        """
        Helper function to calculate ATR using the IndicatorEngine.
        Returns the latest ATR value or 0.0 if calculation fails.
        """
        if ohlcv is None or ohlcv.empty:
            return 0.0

        try:
            # Prepare DataFrame
            df = ohlcv.copy()
            df.columns = [c.lower() for c in df.columns]

            # Ensure datetime index
            if not isinstance(df.index, pd.DatetimeIndex):
                if "ts" in df.columns:
                    df["datetime"] = pd.to_datetime(df["ts"])
                    df.set_index("datetime", inplace=True)
                elif "date" in df.columns:
                    df["datetime"] = pd.to_datetime(df["date"])
                    df.set_index("datetime", inplace=True)

            # Calculate ATR (Default period N=14)
            engine = IndicatorEngine()
            atr_df = engine.calculate(df, ATR, N=14)

            if not atr_df.empty and "atr" in atr_df.columns:
                val = atr_df["atr"].iloc[-1]
                # Handle potential NaN if history is too short
                return val if pd.notna(val) else 0.0

        except Exception as e:
            logger.error(f"ATR Calculation Error: {e}")

        return 0.0

    def calculate_stop_price(self, position_info):
        """
        ATR 止损 (硬逻辑)：价格 - N * ATR
        结构止损 (软逻辑)：由外部传入的 structure_stop（如近期低点、支撑位）
        决策规则：max(p_atr, p_struct)。系统会选择离当前价格更近的那个作为止损点。
        """
        current_price = position_info.get("current_price")
        atr = position_info.get("atr")

        # 如果没有传入atr，尝试使用ohlcv计算
        if atr is None and "ohlcv" in position_info:
            atr = self._calculate_atr_from_ohlcv(position_info["ohlcv"])

        # 如果计算失败或仍为None，则使用initial_atr
        if atr is None or atr == 0:
            atr = self.initial_atr

        # 如果外部没有传入结构止损，则默认为极小值，此时将采用 ATR 止损
        structure_stop = position_info.get("structure_stop", float("-inf"))

        if current_price is None:
            logger.warning("calculate_stop_price: missing current_price")
            return self.stop_price

        if atr is None or atr == 0:
            logger.warning("calculate_stop_price: missing atr and initial_atr is 0")
            return self.stop_price

        # ATR 止损计算
        p_atr = current_price - (self.n_factor * atr)

        # 决策：选择更接近当前价格（更大值）的止损位
        self.stop_price = max(p_atr, structure_stop)

        return self.stop_price

    def get_mode_data(self):
        """返回当前模式的所有属性"""
        return {
            "stop_price": self.stop_price,
            "initial_atr": self.initial_atr,
            "n_factor": self.n_factor,
            "time_stop_days": self.time_stop_days,
            "risk_per_trade": self.risk_per_trade,
            "pyramid_level": self.pyramid_level,
        }

    def check_risk(self, position_info):
        """目前暂不实现"""
        return 0

    def check_reduce_signal(self, position_info):
        """
        检查减持信号 (返回应减持的比例, 1.0表示清仓)
        1. 价格止损：如果 当前价格 <= 止损价格，清仓。
        2. 时间止损 (僵尸仓)：
           - 持仓时间 >= time_stop_days
           - 未触发加仓 (pyramid_level == 0 或 has_added 为 False)
           - 浮盈 <= 0.5 * N * ATR
           满足以上所有条件 -> 清仓。
        """
        current_price = position_info.get("current_price")
        if current_price is None:
            return 0.0

        # 1. 价格判断：触及止损
        if current_price <= self.stop_price:
            logger.info(
                f"Trigger Stop Loss: Current {current_price} <= Stop {self.stop_price}"
            )
            return 1.0

        # 2. 时间判断：僵尸仓
        days_held = position_info.get("days_held", 0)
        # 支持两种标识：pyramid_level > 0 或 has_added = True 表示已加仓
        pyramid_level = position_info.get("pyramid_level", self.pyramid_level)
        has_added = position_info.get("has_added", False) or (pyramid_level > 0)

        # 浮盈可以是总金额，也可以是单股利润。根据公式 0.5 * N * ATR，这里应比较单股利润
        cost_price = position_info.get("cost_price", current_price)
        profit_per_share = current_price - cost_price

        threshold = 0.5 * self.n_factor * self.initial_atr

        if (
            (days_held >= self.time_stop_days)
            and (not has_added)
            and (profit_per_share <= threshold)
        ):
            logger.info(
                f"Trigger Time Stop (Zombie): Days {days_held}, No Add, Profit {profit_per_share:.2f} <= {threshold:.2f}"
            )
            return 1.0

        return 0.0

    def check_add_signal(self, position_info):
        """
        检查增持信号
        触发条件：价格上涨 0.5D (D = 初始风险距离)
        返回：dict { 'signal': bool, 'quantity': float, 'new_stop': float }
        """
        current_price = position_info.get("current_price")
        # P: 初始建仓价格
        first_entry_price = position_info.get("first_entry_price")
        # D: 初始风险距离 (通常由 initial_atr * n_factor 计算，或直接传入)
        # 如果传入了 initial_risk_distance 则优先使用，否则算
        d = position_info.get("initial_risk_distance")
        if d is None:
            if self.initial_atr and self.n_factor:
                d = self.initial_atr * self.n_factor
            else:
                return {}  # 无法计算D

        # 当前层级 (优先使用 position_info 中的，如果没有则使用 self.pyramid_level)
        level = position_info.get("pyramid_level", self.pyramid_level)
        base_quantity = position_info.get("base_quantity", 0)

        if current_price is None or first_entry_price is None or d <= 0:
            return {}

        # 下一加仓触发价：P + (Level + 1) * 0.5 * D
        next_trigger_price = first_entry_price + (level + 1) * 0.5 * d

        if current_price >= next_trigger_price:
            # 触发加仓
            # 计算新的止损位
            new_stop = self.stop_price  # 默认保持

            # L1 (0->1): P - 0.5D
            if level == 0:
                new_stop = first_entry_price - 0.5 * d
            # L2 (1->2): P (保本)
            elif level == 1:
                new_stop = first_entry_price
            # L3 (2->3): P + 0.5D
            elif level == 2:
                new_stop = first_entry_price + 0.5 * d
            # L>2: Current - 1.0D
            else:
                new_stop = current_price - 1.0 * d

            # 铁律：止损只升不降
            final_stop = max(new_stop, self.stop_price)

            logger.info(
                f"Pyramid Signal Level {level}->{level + 1}: Price {current_price} >= Trigger {next_trigger_price}. New Stop {final_stop}"
            )

            return {
                "signal": True,
                "quantity": base_quantity,  # 等额加仓
                "new_stop": final_stop,
                "level": level + 1,
            }

        return {}

    def calculate_purchase_advice(self, data):
        """
        h. 计算购买建议：
        输入 data: {
            'ohlcv': pd.DataFrame (must have 'close', 'high', 'low'),
            'total_capital': float,
            'available_cash': float (optional, defaults to total_capital),
            'current_price': float (optional)
        }
        逻辑：
        1. 风险金额 = Total Capital * risk_per_trade
        2. 计算 ATR 和 止损价 (价格 - N * ATR)
        3. 风险距离 D = N * ATR
        4. 头寸规模 = min(风险金额 / D, 可用现金 / 当前价格)
        """
        ohlcv = data.get("ohlcv")
        total_capital = data.get("total_capital", 0.0)
        available_cash = data.get("available_cash", total_capital)
        current_price = data.get("current_price")

        if ohlcv is None or ohlcv.empty:
            logger.error("calculate_purchase_advice: missing or empty OHLCV data")
            return {}

        if total_capital <= 0:
            logger.error("calculate_purchase_advice: invalid total_capital")
            return {}

        # 1. 计算 ATR
        # Ensure ohlcv has required columns
        ohlcv.columns = [c.lower() for c in ohlcv.columns]
        if "close" not in ohlcv.columns:
            logger.error("calculate_purchase_advice: OHLCV missing 'close' column")
            return {}

        if not current_price:
            current_price = ohlcv["close"].iloc[-1]

        # Use helper
        atr_val = self._calculate_atr_from_ohlcv(ohlcv)

        if atr_val <= 0:
            logger.error("Calculated ATR is <= 0")
            return {}

        # 2. 计算风险距离 D
        d = self.n_factor * atr_val
        if d <= 0:
            return {}

        # 3. 计算止损价格
        stop_price = current_price - d

        # 4. 计算风险金额
        risk_amount = total_capital * self.risk_per_trade

        # 5. 计算头寸规模
        q_risk = int(risk_amount / d)
        q_cash = int(available_cash / current_price)
        quantity = min(q_risk, q_cash)

        return {
            "quantity": quantity,
            "stop_price": stop_price,
            "initial_atr": float(atr_val),
            "risk_distance": float(d),
            "current_price": float(current_price),
            "risk_amount": risk_amount,
            "q_risk": q_risk,
            "q_cash": q_cash,
        }
