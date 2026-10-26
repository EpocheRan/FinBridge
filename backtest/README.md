# Backtest（PyBroker）

回测层基于 [PyBroker](https://www.pybroker.com/)，当前只提供**数据源适配**，策略仍按 PyBroker 的 `exec_fn` 写法放在独立 `.py` 文件中。

## 数据口径

| 列 | 口径 | 用途 |
|---|---|---|
| `close` | 不复权 | 成交、整手、盈亏 |
| `adj_close` | 前复权 | 指标/信号 |
| `volume` | 股 | 量能类因子 |

数据来自 `data_interface.market.loader.MarketDataLoader.ohlc`。回测前请先用现有同步任务把数据灌满 QuestDB；运行期 `_fetch_data` 会只读加载，但缺口仍可能触发 loader 的回源逻辑。

## 最小示例

```python
import numpy as np
import pybroker
from pybroker import Strategy

from data_interface.market.loader import MarketDataLoader
from backtest.data_questdb import QuestDBDataSource


def mom20(data):
    close = data.adj_close
    out = np.full(close.shape, np.nan, dtype=float)
    out[20:] = close[20:] / close[:-20] - 1.0
    return out


MOM20 = pybroker.indicator("mom20", mom20)


def rank(ctx):
    ctx.long_score = ctx.indicator("mom20")[-1]


loader = MarketDataLoader()
source = QuestDBDataSource(loader, freq="d", adjust="none", signal_adjust="qfq")
strategy = Strategy(source, "2020-01-01", "2025-12-31")
strategy.set_max_long_positions(30)
strategy.enable_rotation(worst_rank_held=60)
strategy.add_execution(rank, ["600519", "000001", "300750"], indicators=MOM20)
result = strategy.backtest(warmup=20)

print(result.metrics_df)
print(result.orders.tail())
```

单股 ATR 策略可以直接用 `add_execution(exec_fn, ["600519"])`，并结合 `ctx.stop_loss_pct`、`ctx.stop_trailing`、`ctx.hold_bars`。

## A 股约束

PyBroker 不内置 T+1、涨跌停、100 股整手、停牌撮合和印花税，需要在 `exec_fn` 外层自行校验。建议至少实现：

- T+1：记录最近买入日期，同日禁止卖出；
- 涨停不买、跌停不卖；
- 买入数量向下取整到 100 股；
- 停牌日没有 bar，不产生成交；
- 费用：佣金、卖出印花税、过户费。

## 输出

`TestResult` 提供 `portfolio / positions / orders / trades / metrics_df / bootstrap`，可以直接写入 QuestDB、CSV 或 HTML 报告。
