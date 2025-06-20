import backtrader as bt
import numpy as np
import pandas as pd
from collections import deque

class AlphaFactor(bt.Indicator):
    """因子基类，提供通用接口"""
    lines = ('value',)  # 最终因子值
    
    def __init__(self, *args, **kwargs):
        super().__init__()
        self._setup(*args, **kwargs)
        
    def _setup(self, *args, **kwargs):
        """子类实现因子具体设置"""
        pass
        
    def prenext(self):
        self.lines.value[0] = np.nan
        
    def next(self):
        """子类实现具体计算逻辑"""
        pass
        
    def cross_sectional_rank(self, values):
        """横截面排名"""
        if len(values) < 2:
            return [0.0] * len(values)
        return pd.Series(values).rank(pct=True).values

class Alpha1(AlphaFactor):
    """Alpha1因子完整封装"""
    def _setup(self, std_period=20, argmax_period=5):
        self.params.std_period = std_period
        self.params.argmax_period = argmax_period
        self.addminperiod(self.params.std_period + 1)
        self.signed_power_vals = deque(maxlen=self.params.argmax_period)
        
    def next(self):
        if len(self.data.close) < self.params.std_period + 1:
            self.lines.value[0] = np.nan
            return
            
        # 计算当日收益率
        today_return = (self.data.close[0] / self.data.close[-1]) - 1
        
        # 条件值：收益为负取20日收益标准差，否则取收盘价
        if today_return < 0:
            closes = self.data.close.get(size=self.params.std_period + 1)
            returns = (closes[1:] / closes[:-1]) - 1
            cond_value = np.std(returns)
        else:
            cond_value = self.data.close[0]
        
        # SignedPower: sign(x) * |x|^2
        signed_power = np.sign(cond_value) * (cond_value ** 2)
        
        # 存储并维护最近5天的值
        self.signed_power_vals.appendleft(signed_power)
        
        if len(self.signed_power_vals) < self.params.argmax_period:
            self.lines.value[0] = np.nan
            return
            
        # 计算Ts_ArgMax（找到最大值出现的最远位置）
        max_index = np.argmax(list(self.signed_power_vals))
        
        # 横截面排名
        all_values = []
        for d in self.datas:
            if hasattr(d.alpha1, 'signed_power_vals') and len(d.alpha1.signed_power_vals) >= self.params.argmax_period:
                all_values.append(np.argmax(list(d.alpha1.signed_power_vals)))
        
        if len(all_values) < 2:
            self.lines.value[0] = np.nan
            return
            
        # 计算排名并转换为最终因子值
        ranks = self.cross_sectional_rank(all_values)
        # 找到当前股票在all_values中的索引
        current_idx = [i for i, v in enumerate(all_values) if v == max_index][0]
        self.lines.value[0] = ranks[current_idx] - 0.5

class Alpha2(AlphaFactor):
    """Alpha2因子完整封装"""
    def _setup(self, lookback=6):
        self.params.lookback = lookback
        self.addminperiod(self.params.lookback + 2)
        self.delta_log_volume = deque(maxlen=self.params.lookback)
        self.open_ratio = deque(maxlen=self.params.lookback)
        self.rank_history = deque(maxlen=self.params.lookback)
        
    def log(self, x):
        return np.log(x) if x > 0 else 0
        
    def next(self):
        if len(self.data.volume) < 3:  # 至少需要3天数据计算delta
            self.lines.value[0] = np.nan
            return
            
        # 计算delta(log(volume), 2)
        vol_log = self.log(self.data.volume[0]) - self.log(self.data.volume[-2])
        self.delta_log_volume.append(vol_log)
        
        # 计算(close - open)/open
        oratio = (self.data.close[0] - self.data.open[0]) / (self.data.open[0] + 1e-8)
        self.open_ratio.append(oratio)
        
        # 收集所有股票的原始值
        all_vol = []
        all_oratio = []
        for d in self.datas:
            if hasattr(d.alpha2, 'delta_log_volume') and hasattr(d.alpha2, 'open_ratio'):
                if d.alpha2.delta_log_volume and d.alpha2.open_ratio:
                    all_vol.append(d.alpha2.delta_log_volume[-1])
                    all_oratio.append(d.alpha2.open_ratio[-1])
        
        if len(all_vol) < 2 or len(all_oratio) < 2:
            self.lines.value[0] = np.nan
            return
            
        # 横截面排名
        rank_vol = self.cross_sectional_rank(all_vol)
        rank_oratio = self.cross_sectional_rank(all_oratio)
        
        # 找到当前股票在all_vol中的索引
        current_idx = [i for i, v in enumerate(all_vol) if v == vol_log][0]
        
        # 存储当前排名对
        self.rank_history.append((rank_vol[current_idx], rank_oratio[current_idx]))
        
        # 计算相关系数
        if len(self.rank_history) >= self.params.lookback:
            vol_ranks = [x[0] for x in self.rank_history]
            oratio_ranks = [x[1] for x in self.rank_history]
            
            correlation = np.corrcoef(vol_ranks, oratio_ranks)[0, 1]
            self.lines.value[0] = -1 * correlation if not np.isnan(correlation) else np.nan
        else:
            self.lines.value[0] = np.nan