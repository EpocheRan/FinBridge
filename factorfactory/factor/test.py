import backtrader as bt
import numpy as np
import pandas as pd
from collections import deque

class MultiDataRank(bt.Indicator):
    """
    多数据源排名指标
    计算每个数据源收盘价在给定时间点的排名
    
    参数:
        desc (bool): 排名排序方式(默认True为降序，即最高价为第1名)
    """
    lines = ('ranks',)  # 输出线，存储排名结果
    params = (('std_period', 20), ('argmax_period', 5))  # 参数
    
    # 动态创建线名称，使每个数据源有对应的排名输出
    plotlines = {'ranks': dict(_method='_plot_ranks')}
    
    def __init__(self, *datas):
        super(MultiDataRank, self).__init__()
        self.datas = list(datas)  # 存储所有传入的数据源
        self.rank_array = np.zeros(len(self.datas))  # 创建存储排名的数组
        self.signed_power_vals = deque(maxlen=self.params.argmax_period)
        self.moving_returns = deque(maxlen=self.params.argmax_period)

    def next(self):

        if len(self.data.close) < self.params.std_period + 1:
            self.lines.value[0] = np.nan
            return

        # 计算当日收益率
        today_return = (self.data.close[0] / self.data.close[-1]) - 1
        self.moving_returns.append(today_return)

        # 条件值：收益为负取20日收益标准差，否则取收盘价
        if today_return < 0:
            cond_value = np.std(np.array(self.moving_returns))
        else:
            cond_value = float(self.data.close[0])

        # SignedPower: x**n
        signed_power = cond_value ** 2

        # 存储并维护最近5天的值（因为找最远位置，所以用left）
        self.signed_power_vals.appendleft(signed_power)
        
        if len(self.signed_power_vals) < self.params.argmax_period:
            self.lines.value[0] = np.nan
            return
            
        # 计算Ts_ArgMax（找到最大值出现的最远位置）
        max_index = np.argmax(list(self.signed_power_vals))
        


        # 横截面排名
        all_values = []
        for d in self._owner.datas:
            if hasattr(d, 'alpha1') and len(d.alpha1.signed_power_vals) >= self.params.argmax_period:
                all_values.append(np.argmax(list(d.alpha1.signed_power_vals)))
        
        if len(all_values) < 2:
            self.lines.value[0] = np.nan
            return
            
        # 计算排名并转换为最终因子值
        ranks = self.cross_sectional_rank(all_values)
        # 找到当前股票在all_values中的索引
        current_idx = [i for i, v in enumerate(all_values) if v == max_index][0]
        self.lines.value[0] = ranks[current_idx] - 0.5





        # 收集所有数据源的当前收盘价
        current_closes = [d.close[0] for d in self.datas]
        
        # 使用numpy计算排名
        closes_array = np.array(current_closes)
        ranks = bt.analyzers.get_rank(closes_array, ascending=not self.p.desc)
        
        # 将排名结果存储到lines中
        self.lines.ranks[0] = tuple(ranks)  # 存储为元组
        
        # 同时更新每个数据源的单独排名线
        for i, rank in enumerate(ranks):
            getattr(self.lines, f'rank_{i}')[0] = rank

    def get_rank(self, data_index):
        """获取特定数据源的当前排名"""
        if 0 <= data_index < len(self.datas):
            return getattr(self.lines, f'rank_{data_index}')[0]
        return None

    def get_all_ranks(self):
        """获取所有数据源的当前排名"""
        return self.lines.ranks[0]
    
    def cross_sectional_rank(self, values):
        """横截面排名"""
        if len(values) < 2:
            return [0.0] * len(values)
        return pd.Series(values).rank(pct=True).values
