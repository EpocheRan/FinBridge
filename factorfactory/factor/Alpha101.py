import backtrader as bt
import array
import numpy as np
import pandas as pd
from collections import deque
import matplotlib.pyplot as plt


__all__ = ["alpha1"]

class dailyReturn(bt.Indicator):
    lines = ('return_d',) 

    def __init__(self, *datas):
        super(dailyReturn, self).__init__()
    
    def next(self):
        self.lines.return_d[0] = (self.data.close[0] / self.data.close[-1]) - 1

class alpha1_Ts_ArgMax(bt.Indicator):
    """
    Alpha1 中 Ts_ArgMax 实现：
    rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5
    """
    lines = ('ts_argmax',) 
    params = (('std_period', 20), ('argmax_period', 5), ) 
    
    def __init__(self, *datas):
        super(alpha1_Ts_ArgMax, self).__init__()
        self.daily_return = dailyReturn(self.data)
        self.signed_power_vals = deque(maxlen=self.params.argmax_period)
        self.moving_returns = deque(maxlen=self.params.std_period)

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


class alpha1(bt.Strategy):
    """处理横截面排名"""
    
    def __init__(self):
        # 为每个股票添加Alpha1指标
        self.alpha1_indicators =  [alpha1_Ts_ArgMax(d) for d in self.datas]
        
        # 初始化数据结构
        self.alpha_records = []  # 存储所有记录
        self.current_date = None  # 当前日期

        # 存储因子值的字典
        self.alpha1_values = {}

    def next(self):
        # 获取当前日期
        self.current_date = self.data.datetime.date(0)

        # 收集所有股票的ts_argmax值
        ts_values = []
        valid_data = []
        
        for i, d in enumerate(self.datas):
            ts_val = self.alpha1_indicators[i].ts_argmax[0]
            # 跳过无效数据
            if not np.isnan(ts_val):
                ts_values.append(ts_val)
                valid_data.append(d._name)
        
        # 需要至少2只股票计算排名
        if len(ts_values) >= 2:
            ranks = pd.Series(ts_values).rank(pct=True)
        
        # 分配最终因子值
        for i, name in enumerate(valid_data):
            factor_value = ranks.iloc[i] - 0.5
            self.alpha_records.append({'date': self.current_date,'stock': name,'alpha1': factor_value})

    def stop(self):

        df = pd.DataFrame(self.alpha_records)
        """打印最终因子值"""
        self.plot_histograms(df)

    def plot_histograms(self, df):
        """为每个股票绘制直方图"""
        
        # 按股票分组绘制
        stocks = df['stock'].unique()
        for stock in stocks:
            stock_df = df[df['stock'] == stock]
            
            plt.figure(figsize=(10, 6))
            plt.hist(stock_df['alpha1'], bins=20, alpha=0.7, color='skyblue', edgecolor='black')
            plt.title(f'Alpha1 Distribution for {stock}')
            plt.xlabel('Alpha1 Value')
            plt.ylabel('Frequency')
            plt.grid(True, linestyle='--', alpha=0.7)
            
        
        # 创建组合图
        plt.figure(figsize=(12, 8))
        for i, stock in enumerate(stocks):
            stock_df = df[df['stock'] == stock]
            plt.hist(stock_df['alpha1'], bins=20, alpha=0.5, label=stock)
        
        plt.title('Alpha1 Distribution Comparison')
        plt.xlabel('Alpha1 Value')
        plt.ylabel('Frequency')
        plt.legend()
        plt.grid(True, linestyle='--', alpha=0.7)