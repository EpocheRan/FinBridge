import backtrader as bt
import numpy as np
import pandas as pd
from collections import deque
import logging

# 本地引用
from ..FactorAnalyzer import BaseAnalysisStrategy as BS
from .common import dailyReturn

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Alpha101')

__all__ = ["alpha1"]

class alpha1_Ts_ArgMax(bt.Indicator):
    """
    Alpha1 中 Ts_ArgMax 实现：
    rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5
    """
    name = 'alpha1'
    lines = ('ts_argmax',) 
    params = (('std_period', 20), ('argmax_period', 5), ) 
    
    def __init__(self):
        self.daily_return = dailyReturn(self.data)
        self.signed_power_vals = deque(maxlen=self.params.argmax_period)
        self.moving_returns = deque(maxlen=self.params.std_period)
        super(alpha1_Ts_ArgMax, self).__init__()

    def next(self):
        if len(self.data.close) < self.params.std_period + 1:
            self.lines.ts_argmax[0] = np.nan
            return
        
        # ((returns < 0) ? stddev(returns, 20) : close)
        self.moving_returns.append(self.daily_return)

        # 条件值：收益为负取20日收益标准差，否则取收盘价
        if self.daily_return < 0:
            cond_value = np.std(np.array(self.moving_returns))
        else:
            cond_value = float(self.data.close[0])

        # SignedPower(x, 2.)
        signed_power = cond_value ** 2

        # Ts_ArgMax(x, 5)
        # 存储并维护最近5天的值（因为找最远位置，所以用left）
        self.signed_power_vals.appendleft(signed_power)
        
        if len(self.signed_power_vals) < self.params.argmax_period:
            self.lines.ts_argmax[0] = np.nan
            return
            
        # 计算Ts_ArgMax（找到最大值出现的最远位置）
        self.lines.ts_argmax[0] = np.argmax(list(self.signed_power_vals))

class alpha1(BS):
    """处理横截面排名"""
    
    def __init__(self):
        self.add_factor(alpha1_Ts_ArgMax)
        super(alpha1, self).__init__()

    def next(self):
        super(alpha1, self).next()  # 调用基类的next方法
        
        # 收集所有股票的ts_argmax值
        ts_values = []
        valid_data = []
        
        factor = self.loaded_factors[alpha1_Ts_ArgMax.name]

        for i, d in enumerate(self.datas):
            ts_val = factor[i].ts_argmax[0]
            # 跳过无效数据
            if not np.isnan(ts_val):
                ts_values.append(ts_val)
                valid_data.append(d._name)
        
        # 需要至少2只股票计算排名
        if len(ts_values) >= 2:
            ranks = pd.Series(ts_values).rank(pct=True)
        
        # 分配最终因子值并记录
        for i, stock in enumerate(valid_data):
            alpha1_value = ranks.iloc[i] - 0.5
            self.record_factor(alpha1_Ts_ArgMax.name, stock, alpha1_value)