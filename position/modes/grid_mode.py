"""网格交易模式 (Grid Trading Mode)

思路
----
以 ``base_price`` 为中枢，按 ``grid_step``（或 ``grid_step_pct``）向上下各铺
``grid_count`` 个网格。价格每下穿一格买入 ``quantity_per_grid``，每上穿一格卖出
``quantity_per_grid``；跌破网格下界则清仓止损。适合震荡行情，靠价格来回波动赚差价。

与 ATRMode 一样实现 PositionModeBase 的 8 个接口，因此可被 PositionManager 通过
模式工厂透明调度。
"""

import logging
import math

from .base import PositionModeBase

logger = logging.getLogger("GridMode")


class GridMode(PositionModeBase):
    def __init__(
        self,
        stop_price=0.0,
        base_price=0.0,  # 网格中枢价
        grid_step=0.0,  # 网格间距（绝对价格）
        grid_step_pct=0.0,  # 网格间距（百分比，grid_step 为 0 时生效）
        grid_count=5,  # 上下各几格
        quantity_per_grid=0.0,  # 每格交易数量
        lower_bound=0.0,  # 网格下界（0 表示自动 = base - count*step）
        upper_bound=0.0,  # 网格上界（0 表示自动 = base + count*step）
        max_position=0.0,  # 最大持仓数量（0 表示不限）
        risk_per_trade=0.02,  # 初始建仓风险比例（用于购买建议）
        grid_level=0,  # 最近一次成交所处的网格层级（0 = 中枢）
    ):
        super().__init__(stop_price)
        self.base_price = float(base_price or 0.0)
        self.grid_step = float(grid_step or 0.0)
        self.grid_step_pct = float(grid_step_pct or 0.0)
        self.grid_count = int(grid_count or 5)
        self.quantity_per_grid = float(quantity_per_grid or 0.0)
        self.lower_bound = float(lower_bound or 0.0)
        self.upper_bound = float(upper_bound or 0.0)
        self.max_position = float(max_position or 0.0)
        self.risk_per_trade = float(risk_per_trade or 0.02)
        self.grid_level = int(grid_level or 0)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _step(self) -> float:
        """网格间距（绝对价格）。优先 grid_step，其次按 base_price 的百分比。"""
        if self.grid_step > 0:
            return self.grid_step
        if self.grid_step_pct > 0 and self.base_price > 0:
            return self.base_price * self.grid_step_pct
        return 0.0

    def _bounds(self) -> tuple[float, float]:
        step = self._step()
        lower = self.lower_bound
        upper = self.upper_bound
        if lower <= 0 and step > 0:
            lower = self.base_price - self.grid_count * step
        if upper <= 0 and step > 0:
            upper = self.base_price + self.grid_count * step
        # 下界可能被算成 <= 0（价格跌破 0 不现实）：此时视为「没有下界止损」
        if lower <= 0:
            logger.warning(
                "网格下界 <= 0（base=%.4f step=%.4f count=%s），将不设价格止损",
                self.base_price,
                step,
                self.grid_count,
            )
        return float(max(lower, 0.0)), float(upper)

    def _level_of(self, price: float) -> int:
        """价格所在的网格层级（相对 base_price，向下取整到整格）。"""
        step = self._step()
        if step <= 0 or self.base_price <= 0:
            return self.grid_level
        # 用 floor 而非 round：round 在 ±0.5 处有银行家舍入，会让上下穿判断不对称
        return int(math.floor((price - self.base_price) / step + 1e-9))

    # ------------------------------------------------------------------ #
    # a. 计算止损价
    # ------------------------------------------------------------------ #
    def calculate_stop_price(self, position_info):
        lower, _ = self._bounds()
        if lower <= 0:
            # 下界落到 0：不设止损（不要把 0 当成有效止损价落库）
            return 0.0
        # 显式 stop_price 与网格下界取更高者（更贴近当前价的保护）
        stop = max(self.stop_price, lower) if self.stop_price else lower
        self.stop_price = float(stop or 0.0)
        return self.stop_price

    # ------------------------------------------------------------------ #
    # b. 模式数据
    # ------------------------------------------------------------------ #
    def get_mode_data(self):
        lower, upper = self._bounds()
        return {
            "stop_price": self.stop_price,
            "base_price": self.base_price,
            "grid_step": self._step(),
            "grid_step_pct": self.grid_step_pct,
            "grid_count": self.grid_count,
            "quantity_per_grid": self.quantity_per_grid,
            "lower_bound": lower,
            "upper_bound": upper,
            "max_position": self.max_position,
            "risk_per_trade": self.risk_per_trade,
            "grid_level": self.grid_level,
        }

    # ------------------------------------------------------------------ #
    # e. 风险等级 0-5（越靠近/跌破下界越高）
    # ------------------------------------------------------------------ #
    def check_risk(self, position_info):
        price = position_info.get("current_price")
        if price is None:
            return 0
        lower, upper = self._bounds()
        if lower <= 0 or upper <= lower:
            return 0
        pos = (price - lower) / (upper - lower)  # 0=下界, 1=上界
        if pos <= 0:
            return 5
        if pos < 0.2:
            return 4
        if pos < 0.4:
            return 3
        if pos < 0.6:
            return 2
        if pos < 0.8:
            return 1
        return 0

    # ------------------------------------------------------------------ #
    # f. 减持信号（返回应减持比例 0-1）
    # ------------------------------------------------------------------ #
    def check_reduce_signal(self, position_info):
        price = position_info.get("current_price")
        if price is None:
            return 0.0

        # 1. 跌破止损/下界 -> 清仓
        if self.stop_price and price <= self.stop_price:
            logger.info(f"Grid stop hit: {price} <= {self.stop_price}")
            return 1.0

        # 2. 上穿一格 -> 卖出 quantity_per_grid（换算为持仓比例）
        level = self._level_of(price)
        if level <= self.grid_level:
            return 0.0
        qty = float(position_info.get("quantity") or 0)
        per = self.quantity_per_grid
        if qty > 0 and per > 0:
            return min(1.0, per / qty)
        return 0.0

    # ------------------------------------------------------------------ #
    # g. 增持信号
    # ------------------------------------------------------------------ #
    def check_add_signal(self, position_info):
        price = position_info.get("current_price")
        if price is None:
            return {}
        step = self._step()
        if step <= 0 or self.base_price <= 0:
            return {}

        level = self._level_of(price)
        if level >= self.grid_level:
            return {}  # 未下穿新的买入格

        crossed = self.grid_level - level  # 本次跨越的格数
        qty = self.quantity_per_grid * crossed
        if qty <= 0:
            return {}

        # max_position 约束
        held = float(position_info.get("quantity") or 0)
        if self.max_position > 0:
            room = self.max_position - held
            if room <= 0:
                return {}
            qty = min(qty, room)

        return {
            "signal": True,
            "quantity": qty,
            "level": level,
            "grid_price": self.base_price + level * step,
            "new_stop": self.stop_price,
        }

    # ------------------------------------------------------------------ #
    # h. 购买建议（网格首次建仓）
    # ------------------------------------------------------------------ #
    def calculate_purchase_advice(self, data):
        ohlcv = data.get("ohlcv")
        total_capital = data.get("total_capital", 0.0)
        available_cash = data.get("available_cash", total_capital)
        current_price = data.get("current_price")

        if ohlcv is None or ohlcv.empty:
            logger.error("GridMode.calculate_purchase_advice: missing OHLCV")
            return {}
        ohlcv = ohlcv.copy()
        ohlcv.columns = [c.lower() for c in ohlcv.columns]
        if "close" not in ohlcv.columns:
            return {}

        if not current_price:
            current_price = float(ohlcv["close"].iloc[-1])
        current_price = float(current_price)
        if current_price <= 0:
            return {}

        # 未设置中枢价时，以最新价作为中枢
        if self.base_price <= 0:
            self.base_price = current_price
        step = self._step()
        if step <= 0:
            # 默认 2% 网格
            step = self.base_price * 0.02
            self.grid_step = step

        lower, upper = self._bounds()
        total_grids = 2 * self.grid_count + 1

        # 每格数量未指定时，按可用资金均分到所有网格
        per = self.quantity_per_grid
        if per <= 0:
            try:
                per = int((float(available_cash) / current_price) / total_grids)
            except (TypeError, ValueError, ZeroDivisionError):
                logger.warning("网格每格数量计算失败（cash=%s price=%s）", available_cash, current_price)
                per = 0
        per = max(0, int(per))

        # 首仓建议：先建 1 格底仓（保守滚动建仓）
        quantity = per
        if self.max_position > 0:
            quantity = min(quantity, int(self.max_position))

        return {
            "quantity": quantity,
            "quantity_per_grid": per,
            "stop_price": float(lower),
            "base_price": self.base_price,
            "grid_step": float(step),
            "grid_count": self.grid_count,
            "lower_bound": float(lower),
            "upper_bound": float(upper),
            "current_price": current_price,
            "risk_amount": total_capital * self.risk_per_trade,
        }

    # ------------------------------------------------------------------ #
    # i. 成交后推进层级（网格：成交价落在哪一格，当前层级就推进到哪一格）
    # ------------------------------------------------------------------ #
    def advance_level(self, trade_type, price, quantity, had_position=False):
        self.grid_level = self._level_of(float(price))
        logger.info(
            f"Grid level advanced to {self.grid_level} (price={price}, {trade_type})"
        )
        return self.grid_level
