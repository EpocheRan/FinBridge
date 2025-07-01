import backtrader as bt
import numpy as np
import pandas as pd
from collections import deque
import matplotlib.pyplot as plt
import logging

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('factor')


__all__ = ["alpha1"]

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


class BaseAnalysisStrategy(bt.Strategy):
    """
    基础Alpha策略类，提供因子记录和未来收益计算功能
    """
    params = (
        ('return_periods', [10, 30, 90]),  # 计算未来收益的周期
        ('zscore_window', None),           # Z-score标准化窗口，None表示全历史
    )
    
    def __init__(self):
        # 初始化记录表头
        column={'datetime':pd.Series(dtype='object'), 
                'stock':pd.Series(dtype='str'), 
                'raw_factor':pd.Series(dtype='float'), 
                'zscore':pd.Series(dtype='float')
        }

        # 为每个数据源添加收益指标, return_periods
        self.return_indicators = {}
        for d in self.datas:
            name = d._name
            self.return_indicators[name] = {}
            for period in self.params.return_periods:
                self.return_indicators[name][f'return_{period}d'] = dailyReturn(d, period=period)
    
        for period in self.params.return_periods: column[f'return_{period}d'] = pd.Series(dtype='float')

        msg = [f'return_{period}d' for period in self.params.return_periods]
        msg = ', '.join(msg)
        logger.info("载入收益指标" + msg)

        # 初始化记录df
        self.alpha_records = pd.DataFrame(column)
        self.current_date = None
        
    def record_factor(self, stock, raw_factor):
        """记录因子原始值并计算Z-score,return"""
        # 获取当前日期
        current_datetime = self.data.datetime.date(0)
        
        # 获取各种收益数据
        returns = {}
        for period in self.params.return_periods:
            key = f'return_{period}d'
            val = self.return_indicators[stock][key][0]
            returns[key] = val if not np.isnan(val) else np.nan

        # 计算Z-score
        zscore = self.calculate_zscore(stock, raw_factor, current_datetime)
        
        # 记录因子值
        new_row =  pd.DataFrame([{
            'datetime': current_datetime,
            'stock': stock,
            'raw_factor': raw_factor,
            'zscore': zscore,
            **returns
        }])

        self.alpha_records = pd.concat([self.alpha_records, new_row], ignore_index=True)
    
    def calculate_zscore(self, stock, raw_factor, current_datetime):
        """计算Z-score标准化值"""
        # 收集历史因子值
        r = self.alpha_records 
        history = r[(r['stock'] == stock) & (r['datetime'] < current_datetime)]['raw_factor']
        
        # 如果有窗口限制，只保留最近的记录
        if self.params.zscore_window is not None:
            history = history[-self.params.zscore_window:]
        
        # 计算Z-score
        if len(history) > 1:
            mean_val = np.mean(history)
            std_val = np.std(history)
            if std_val > 0:
                return (raw_factor - mean_val) / std_val
        
        # 如果没有足够的历史数据，返回NaN
        return np.nan   
    
    def next(self):
        pass
    
    def plot_results(self):
        """绘制结果（可被子类覆盖）"""
        df = self.alpha_records
        df = df.fillna(0)
        
        # 绘制因子分布直方图
        stocks = df['stock'].unique()
        for stock in stocks:
            stock_df = df[df['stock'] == stock]
            
            plt.figure(figsize=(10, 6))
            plt.hist(stock_df['raw_factor'], bins=20, alpha=0.7, color='skyblue', edgecolor='black', label='Raw Factor')
            plt.hist(stock_df['zscore'], bins=20, alpha=0.7, color='salmon', edgecolor='black', label='Z-score')
            plt.title(f'Factor Distribution for {stock}')
            plt.xlabel('Value')
            plt.ylabel('Frequency')
            plt.legend()
            plt.grid(True, linestyle='--', alpha=0.7)
        
        # 绘制未来收益分布
        for period in self.params.return_periods:
            plt.figure(figsize=(10, 6))
            for stock in stocks:
                plt.hist(df[df['stock'] == stock][f'return_{period}d'], bins=20, alpha=0.5, label=stock)
            
            plt.title(f'{period}-Day Future Return Distribution')
            plt.xlabel('Return')
            plt.ylabel('Frequency')
            plt.legend()
            plt.grid(True, linestyle='--', alpha=0.7)
        
        plt.show()
    
    def stop(self):
        """回测结束时调用"""
        self.plot_results()


class alpha1(BaseAnalysisStrategy):
    """处理横截面排名"""
    
    def __init__(self):
        super(alpha1, self).__init__()
        # 为每个股票添加Alpha1指标
        self.alpha1_indicators = [alpha1_Ts_ArgMax(d) for d in self.datas]
        logger.info("载入Ts_ArgMax指标")

    def next(self):
        super(alpha1, self).next()  # 调用基类的next方法
        
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
        
        # 分配最终因子值并记录
        for i, name in enumerate(valid_data):
            factor_value = ranks.iloc[i] - 0.5
            self.record_factor(name, factor_value)