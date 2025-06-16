import backtrader as bt
from scipy.stats import rankdata

# ======================
# 基础函数库 (Alpha工具集)
# ======================
class AlphaTools:
    @staticmethod
    def delta(data, period):
        """计算当前值与period天前值的差值"""
        return data - data(-period)

    @staticmethod
    def ts_argmax(data, period):
        """返回窗口期内最大值的位置(0=当天,1=前一天)"""
        return bt.indicators.ArgMax(data, period)

    @staticmethod
    def signed_power(data, exp):
        """带符号的幂运算: sign(x) * |x|^exp"""
        return bt.If(data >= 0, (data)**exp, -((-data)**exp))

    @staticmethod
    def ts_rank(data, period):
        """时间序列排名 (当前值在窗口期内的百分位排名)"""
        class TsRank(bt.Indicator):
            lines = ('rank',)
            params = (('period', period),)
            
            def __init__(self):
                self.addminperiod(self.p.period)
            
            def next(self):
                window = self.data.get(size=self.p.period)
                self.lines.rank[0] = rankdata(window)[-1] / float(self.p.period)
        return TsRank(data)()
    
    @staticmethod
    def correlation(x, y, period):
        """两个序列的相关系数"""
        return bt.indicators.Correlation(x, y, period)
    
    @staticmethod
    def stddev(data, period):
        """标准差"""
        return bt.indicators.StdDev(data, period)
    
    @staticmethod
    def ts_min(data, period):
        """时间窗口最小值"""
        return bt.indicators.Lowest(data, period)
    
    @staticmethod
    def ts_max(data, period):
        """时间窗口最大值"""
        return bt.indicators.Highest(data, period)

    @staticmethod
    def rank(data):
        """横截面排名 (跨股票) - 需要多数据源支持"""
        # 在单股票策略中简化为标准化
        return (data - bt.indicators.SMA(data, period=20)) / bt.indicators.StdDev(data, period=20)

# ======================
# Alpha 指标实现 (1-10)
# ======================
class Alpha1(bt.Indicator):
    """rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5"""
    lines = ('alpha',)
    
    def __init__(self):
        # 计算中间指标
        returns = (self.data.close / self.data.close(-1)) - 1
        condition = returns < 0
        std_ret = self.stddev(returns, period=20)
        selected = bt.If(condition, std_ret, self.data.close)
        sp = self.signed_power(selected, 2.0)
        argmax = self.ts_argmax(sp, period=5)
        
        # 最终计算
        self.lines.alpha = self.rank(argmax) - 0.5
        super(Alpha1, self).__init__()

class Alpha2(bt.Indicator):
    """-1 * correlation(rank(delta(log(volume), 2)), rank(((close - open) / open)), 6)"""
    lines = ('alpha',)
    
    def __init__(self):
        # 计算中间指标
        log_vol = bt.log(self.data.volume)
        delta_log_vol = self.delta(log_vol, period=2)
        price_ratio = (self.data.close - self.data.open) / self.data.open
        
        # 计算排名
        rank1 = self.rank(delta_log_vol)
        rank2 = self.rank(price_ratio)
        
        # 计算相关性
        corr = self.correlation(rank1, rank2, period=6)
        
        self.lines.alpha = -1 * corr
        super(Alpha2, self).__init__()

class Alpha3(bt.Indicator):
    """-1 * correlation(rank(open), rank(volume), 10)"""
    lines = ('alpha',)
    
    def __init__(self):
        rank_open = self.rank(self.data.open)
        rank_vol = self.rank(self.data.volume)
        corr = self.correlation(rank_open, rank_vol, period=10)
        self.lines.alpha = -1 * corr
        super(Alpha3, self).__init__()

class Alpha4(bt.Indicator):
    """-1 * Ts_Rank(rank(low), 9)"""
    lines = ('alpha',)
    
    def __init__(self):
        rank_low = self.rank(self.data.low)
        ts_rank = self.ts_rank(rank_low, period=9)
        self.lines.alpha = -1 * ts_rank
        super(Alpha4, self).__init__()

class Alpha5(bt.Indicator):
    """rank((open - (sum(vwap, 10) / 10)) * (-1 * abs(rank((close - vwap)))"""
    lines = ('alpha',)
    
    def __init__(self):
        sma_vwap = bt.indicators.SMA(self.data.vwap, period=10)
        part1 = self.rank(self.data.open - sma_vwap)
        
        diff = self.data.close - self.data.vwap
        part2 = -1 * bt.Abs(self.rank(diff))
        
        self.lines.alpha = part1 * part2
        super(Alpha5, self).__init__()

class Alpha6(bt.Indicator):
    """-1 * correlation(open, volume, 10)"""
    lines = ('alpha',)
    
    def __init__(self):
        corr = self.correlation(self.data.open, self.data.volume, period=10)
        self.lines.alpha = -1 * corr
        super(Alpha6, self).__init__()

class Alpha7(bt.Indicator):
    """((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : -1"""
    lines = ('alpha',)
    
    def __init__(self):
        # 计算ADV20
        adv20 = bt.indicators.SMA(self.data.volume * self.data.vwap, period=20)
        
        # 条件判断
        condition = adv20 < self.data.volume
        
        # True分支计算
        delta_close = self.delta(self.data.close, period=7)
        abs_delta = bt.Abs(delta_close)
        ts_rank_val = self.ts_rank(abs_delta, period=60)
        sign_val = bt.If(delta_close >= 0, 1, -1)
        true_val = (-1 * ts_rank_val) * sign_val
        
        # False分支
        false_val = -1
        
        self.lines.alpha = bt.If(condition, true_val, false_val)
        super(Alpha7, self).__init__()

class Alpha8(bt.Indicator):
    """-1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5) * sum(returns, 5)), 10)))"""
    lines = ('alpha',)
    
    def __init__(self):
        # 计算5日开盘价和收益率的和
        sum_open = bt.indicators.Sum(self.data.open, period=5)
        returns = (self.data.close / self.data.close(-1)) - 1
        sum_returns = bt.indicators.Sum(returns, period=5)
        
        # 计算当前值和10天前值
        product = sum_open * sum_returns
        delayed_product = product(-10)
        
        # 计算差异并排名
        diff = product - delayed_product
        self.lines.alpha = -1 * self.rank(diff)
        super(Alpha8, self).__init__()

class Alpha9(bt.Indicator):
    """((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : 
        ((ts_max(delta(close, 1), 5) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))"""
    lines = ('alpha',)
    
    def __init__(self):
        delta_close = self.delta(self.data.close, period=1)
        min_delta = self.ts_min(delta_close, period=5)
        max_delta = self.ts_max(delta_close, period=5)
        
        condition1 = min_delta > 0
        condition2 = max_delta < 0
        
        self.lines.alpha = bt.If(
            condition1, 
            delta_close,
            bt.If(condition2, delta_close, -1 * delta_close)
        )
        super(Alpha9, self).__init__()

class Alpha10(bt.Indicator):
    """rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : 
        ((ts_max(delta(close, 1), 4) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))"""
    lines = ('alpha',)
    
    def __init__(self):
        delta_close = self.delta(self.data.close, period=1)
        min_delta = self.ts_min(delta_close, period=4)
        max_delta = self.ts_max(delta_close, period=4)
        
        condition1 = min_delta > 0
        condition2 = max_delta < 0
        
        result = bt.If(
            condition1, 
            delta_close,
            bt.If(condition2, delta_close, -1 * delta_close)
        )
        
        self.lines.alpha = self.rank(result)
        super(Alpha10, self).__init__()


__all__ = ['Alpha1', 'Alpha2', 'Alpha3'] 