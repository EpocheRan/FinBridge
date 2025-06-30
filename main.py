import backtrader as bt
from utils import DataLoader
from factorfactory import factor
import numpy as np
import pandas as pd


# 主程序
if __name__ == "__main__":

    cerebro = bt.Cerebro()
    
    # 添加多个股票数据（示例）
    stocks = ['AAPL', 'MSFT', 'GOOG', 'AMZN', 'META']
    for symbol in stocks:
        # 创建模拟数据
        dates = pd.date_range('2023-01-01', periods=100)
        data = pd.DataFrame({
            'open': np.random.rand(100) * 100 + 100,
            'high': np.random.rand(100) * 10 + 105,
            'low': np.random.rand(100) * 10 + 95,
            'close': np.random.rand(100) * 100 + 100,
            'volume': np.random.randint(100000, 1000000, 100)
        }, index=dates)
        
        # 添加到Backtrader
        bt_data = bt.feeds.PandasData(
            dataname=data,
            name=symbol
        )
        cerebro.adddata(bt_data)
    
    # 使用正确的策略类
    cerebro.addstrategy(factor.alpha1)
    
    cerebro.broker.setcash(100000.0)
    cerebro.broker.setcommission(commission=0.001) 
    
    print('Starting Portfolio Value: %.2f' % cerebro.broker.getvalue())
    cerebro.run()
    print('Final Portfolio Value: %.2f' % cerebro.broker.getvalue())
    
    # 绘制结果
    cerebro.plot(style='candlestick')