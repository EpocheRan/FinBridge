import backtrader as bt
from utils import DataLoader
from factorfactory import factor


# 布林带策略
class BollingerBandStrategy(bt.Strategy):
    params = (
        ('period', 20),
        ('devfactor', 1.5),
    )

    def __init__(self):
        self.boll = bt.indicators.BollingerBands(
            self.data.close, 
            period=self.params.period, 
            devfactor=self.params.devfactor
        )
        self.order = None

    def next(self):
        if self.order:
            return  # 有未处理订单则返回
        
        # 没有持仓
        if not self.position:
            # 收盘价下穿1.5倍标准差下轨 - 买入
            if self.data.close[0] < self.boll.lines.bot[0]:
                if self.data.close[0] == 0: return
                self.order = self.buy(size=1000 // self.data.close[0])
        else:
            # 收盘价上穿1.5倍标准差上轨 - 卖出
            if self.data.close[0] > self.boll.lines.top[0]:
                self.order = self.sell(size=self.position.size)

    def notify_order(self, order):
        if order.status in [order.Completed]:
            self.order = None  # 重置订单状态



# 回测和结果分析
def run_backtest(data):
    cerebro = bt.Cerebro()
    
    # 添加数据
    data_feed = bt.feeds.PandasData(dataname = data)
    cerebro.adddata(data_feed)
    
    # 添加策略
    cerebro.addstrategy(BollingerBandStrategy)
    
    # 设置初始资金和手续费
    cerebro.broker.setcash(10000)
    cerebro.broker.setcommission(commission=0.001)  # 0.1%佣金
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe_ratio')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    
    # 运行回测
    results = cerebro.run()
    
    # 打印结果
    strat = results[0]
    sharpe = strat.analyzers.sharpe_ratio.get_analysis()['sharperatio']
    returns = strat.analyzers.returns.get_analysis()['rnorm100']
    
    print('=' * 50)
    print(f"初始资金: {10000:.2f}")
    print(f"最终资金: {cerebro.broker.getvalue():.2f}")
    print(f"夏普比率: {sharpe:.4f}")
    print(f"年化收益率: {returns:.2f}%")
    print('=' * 50)
    
    # 绘制结果
    cerebro.plot(style='candlestick', volume=False)

# 主程序
if __name__ == "__main__":

    # 步骤1: 加载数据
    DL = DataLoader()
    data = DL.get_stock_data("600519","2010-06-01", "2025-06-10", "d")
    
    # 步骤2: 运行策略和回测
    run_backtest(data)