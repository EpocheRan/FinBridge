from __future__ import annotations

import numpy as np

from factors import F
from strategies.base import SingleSymbolStrategy
from strategies.context import StrategyContext


class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ("600519",)
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"]), F.ma(20)]

    def on_bar(self, ctx: StrategyContext) -> None:
        ma20 = ctx.f("ma20")
        atr = ctx.f(f"atr{int(self.params['n'])}")
        if not np.isfinite(ma20) or not np.isfinite(atr) or atr <= 0:
            return
        # 因子在 hfq 上计算，趋势比较必须用 hfq_close；执行/止损仍用 raw close。
        if ctx.pos == 0 and ctx.hfq_close > ma20:
            scale = ctx.close / ctx.hfq_close if ctx.hfq_close > 0 else 1.0
            ctx.target_weight(
                0.95,
                stop_loss=ctx.close - float(self.params["k"]) * atr * scale,
            )
        elif ctx.pos > 0 and ctx.hfq_close < ma20:
            ctx.close_position()


STRATEGY = AtrTrend
