import backtrader as bt
import numpy as np
import pandas as pd
from collections import deque
import matplotlib.pyplot as plt
import logging
import pyarrow as pa
import pyarrow.parquet as pq
import os

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


class BaseAnalysisStrategy(bt.Strategy):
    """
    基础Analysis策略类，提供因子记录和未来收益计算功能
    """
    params = (
        ('return_periods', [10, 30, 90]),       # 计算未来收益的周期
        ('zscore_window', None),                # Z-score标准化窗口，None表示全历史
        ('parquet_path', './analysis_results'), # Parquet存储路径
        ('max_memory', 10000)                   # 内存最大缓存行数, 为None时不缓存
    )

    factors = [] # 该Strategy载入的indicator，支持多个indicator

    # 记录数据的dataframe的默认表头，需要在继承的子类add_factor方法之前完成
    default_column = {
            'datetime':pd.Series(dtype='object'), 
            'stock':pd.Series(dtype='str')
        }
    
    # 用于缓存计数
    record_count = 0

    def __init__(self):
        # 创建存储目录
        os.makedirs(self.params.parquet_path, exist_ok=True)

        # 为每个数据源添加收益指标
        self.add_return_indicator()
        self.loaded_factors = {}

        # 创建df用于记录：因子计算结果，因子z-score，return
        self.data_records = pd.DataFrame(self.default_column)
        self.load_factor()

    def add_return_indicator(self):
        self.return_indicators = {}
        for d in self.datas:
            name = d._name
            self.return_indicators[name] = {}
            for period in self.params.return_periods:
                self.return_indicators[name][f'return_{period}d'] = dailyReturn(d, period=period)
    
        for period in self.params.return_periods: self.default_column[f'return_{period}d'] = pd.Series(dtype='float')
        logger.info(f"收益指标加载完成: {list(self.params.return_periods)}")

    def add_factor(self, factor_class:bt.Indicator):
        factor_name = factor_class.name
        self.factors.append(factor_class)
        self.default_column[f'{factor_name}_raw'] = []
        self.default_column[f'{factor_name}_zscore'] = []
        logger.info(f"因子 {factor_name} 初始化完成")

    def load_factor(self):
        for d in self.datas:
            for factor in self.factors:
                try:
                    self.loaded_factors[factor.name] = [factor(d) for d in self.datas]
                    
                except:
                    logger.error(f"数据源加载因子 {factor.name} 错误")
        logger.info(f"数据源加载因子 {factor.name} 完成")

    def calculate_returns(self, stock):
        """记录收益数据"""
        returns = {}
        for period in self.params.return_periods:
            key = f'return_{period}d'
            try:
                val = self.return_indicators[stock][key][0]
                returns[key] = val if not np.isnan(val) else np.nan
            except:
                returns[key] = np.nan
        return returns

    def calculate_zscore(self, stock, factor_name, raw_factor, current_datetime):
        """计算Z-score标准化值"""
        # 收集历史因子值
        r = self.data_records 
        history = r[(r['stock'] == stock) & (r['datetime'] < current_datetime)][f'{factor_name}_raw']
        
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

    def record_factor(self, factor_name, stock, raw_value):
        """记录因子值到内存缓存"""
        current_datetime = self.data.datetime.date(0)
        
        # 记录因子值
        self.data_records.loc[(self.data_records['stock'] == stock) & (self.data_records['datetime'] == current_datetime), f'{factor_name}_raw'] = raw_value
        z_score = self.calculate_zscore(stock, factor_name, raw_value, current_datetime)
        self.data_records.loc[(self.data_records['stock'] == stock) & (self.data_records['datetime'] == current_datetime), f'{factor_name}_zscore'] = z_score
        
        # 内存控制
        
        if (self.params.max_memory != None) and (self.record_count >= self.params.max_memory):
            self.flush_to_parquet()

    def flush_to_parquet(self, all=False):
        """将内存数据写入Parquet文件"""
        if self.record_count == 0:
            return
        n = int(self.params.max_memory / 2)
        try:           
            # 转换为PyArrow Table
            if all:
                table = pa.Table.from_pandas(self.data_records)
            else:    
                table = pa.Table.from_pandas(self.data_records[:n])
            
            # 存储数据
            pq.write_to_dataset(
                table,
                root_path=self.params.parquet_path,
                existing_data_behavior='delete_matching'
            )
            
            logger.info(f"写入 {len(self.data_records)} 行数据到 {self.params.parquet_path}")
            
            # 重置内存缓存
            self.data_records.drop(self.data_records.index[:n], inplace=True)
            self.record_count = 0
                
        except Exception as e:
            logger.error(f"写入Parquet失败: {str(e)}") 
    
    def next(self):
        # 在执行next时，为每个数据源创建新的一行
        for d in self.datas:
            stock_returns = self.calculate_returns(d._name)
            current_datetime = self.data.datetime.date(0)
            new_row =  pd.DataFrame([{
                'datetime': current_datetime,
                'stock': d._name,
                **stock_returns
            }])
            self.data_records = pd.concat([self.data_records, new_row], ignore_index=True)

    
    def plot_results(self):
        """绘制结果（可被子类覆盖）"""
        df = self.data_records
        df = df.fillna(0)
        
        # 绘制因子分布直方图
        stocks = df['stock'].unique()
        for stock in stocks:
            stock_df = df[df['stock'] == stock]
            
            plt.figure(figsize=(10, 6))
            plt.hist(stock_df['alpha1_raw'], bins=20, alpha=0.7, color='skyblue', edgecolor='black', label='Raw Factor')
            plt.hist(stock_df['alpha1_zscore'], bins=20, alpha=0.7, color='salmon', edgecolor='black', label='Z-score')
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
        if self.params.max_memory != None: self.flush_to_parquet(all=False)
        self.plot_results()

class alpha1(BaseAnalysisStrategy):
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