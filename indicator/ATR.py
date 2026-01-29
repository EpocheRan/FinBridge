import backtrader as bt
import logging

logger = logging.getLogger("ATR")


class ATR(bt.Indicator):
    lines = ("atr",)
    params = (
        ("N", 14),
        ("method", "EMA"),
    )

    def __init__(self):
        tr = bt.indicators.TrueRange(self.data)

        if self.params.method.upper() == "SMA":
            self.lines.atr = bt.indicators.SimpleMovingAverage(tr, period=self.params.N)
        elif self.params.method.upper() == "EMA":
            self.lines.atr = bt.indicators.ExponentialMovingAverage(
                tr, period=self.params.N
            )
        elif self.params.method.upper() == "WMA":
            self.lines.atr = bt.indicators.WeightedMovingAverage(
                tr, period=self.params.N
            )
        else:
            logger.warning(f"Unknown method {self.params.method}, defaulting to SMA")
            self.lines.atr = bt.indicators.SimpleMovingAverage(tr, period=self.params.N)
