import backtrader as bt
import numpy as np


__all__ = ["dailyReturn"]

class dailyReturn(bt.Indicator):
    lines = ('return_d',)
    params = (('period', 0),)  # 新增 period 参数

    def __init__(self, *datas):
        super(dailyReturn, self).__init__()
        self.period = self.params.period
        if self.period == 0: self.period = -1
    
    def next(self):
        if self.period < 0:  # 过去收益
            lookback = abs(self.period)
            if len(self.data.close) > lookback:
                self.lines.return_d[0] = (self.data.close[0] / self.data.close[self.period]) - 1
            else:
                self.lines.return_d[0] = np.nan

        elif self.period > 0:  # 未来收益
            try:
                self.lines.return_d[0] = (self.data.close[self.period] / self.data.close[0]) - 1
            except:
                self.lines.return_d[0] = np.nan