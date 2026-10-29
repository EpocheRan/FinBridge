# 07 · backtest（回测引擎）

> 详细设计见 [../backtest/ARCHITECTURE.md](../backtest/ARCHITECTURE.md)，用法见
> [../backtest/README.md](../backtest/README.md)。本篇只给架构定位与核心 API 速查。

## 1. 架构与依赖

### 1.1 职责边界

`backtest` 是**唯一的撮合与资金曲线实现**：给定策略 + 行情 + 因子，产出订单、成交、持仓、
净值与绩效指标。它保证 A 股规则：T+1、整手、涨跌停不可成交、停牌、印花税/佣金/过户费。

`PyBroker` 是 **optional extra**（`uv sync --extra backtest`），MCP 运行时不加载。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `strategies` | 策略定义（`BaseStrategy` 子类 / `STRATEGY` 模块变量） |
| 上游 | `factors` | 因子面板（`build_panel`） |
| 上游 | `factor_engine` | 读 STORED 因子 |
| 上游 | `data_interface` | 行情、交易日历 |
| 下游 | `output/backtest/<run_id>/` | orders/trades/equity/metrics |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `application/runner.py` | `BacktestRunner`：`run` / `optimize` / `walkforward` |
| `application/loader.py` | 加载策略模块的 `STRATEGY` |
| `application/cross_section.py` | 横截面混合架构（向量化选股 + PyBroker 执行候选集） |
| `config.py` | `BacktestSettings.from_dict` |
| `domain/models.py` | `RunSpec` / `ExecutionRules` / `DataBundle` / `FillMode` |
| `infrastructure/engine/pybroker_engine.py` | PyBroker 封装 |
| `infrastructure/data/questdb.py` | 只读行情源 |
| `execution/ledger.py` | 自建账本 `PortfolioLedger` |
| `research/service.py` | 因子研究（IC/分层/换手/衰减） |

### 1.4 数据流

```
BacktestRunner.run(strategy_path)
   ├─ loader 加载 STRATEGY → BaseStrategy 实例
   ├─ _resolve_universe（explicit / all_a / index）
   ├─ 取行情 + 因子（stored 优先，AUTO 回退 compute）
   ├─ 引擎执行（单股 or 横截面）
   └─ 账本 → metrics → output/backtest/<run_id>/
```

## 2. 核心类与方法

### 2.1 类表

| 类 | 关键方法/字段 | 说明 |
|---|---|---|
| `BacktestRunner` | `run(path)` / `optimize(path)` / `walkforward(path)` | 单次 / 网格 / 滚动 |
| `BacktestSettings` | `from_dict(payload)` | 全部配置项 + 校验 |
| `RunSpec` | dataclass | 一次运行的完整规格 |
| `ExecutionRules` | `lot_size/t_plus_1/limit_up_down/suspension/commission_rate/stamp_tax_schedule` | 撮合规则 |
| `PortfolioLedger` | 持仓、现金、成交 | 多标的账本 |

关键默认值：`fill="next_open"`、`rebalance="weekly"`、`warmup_min_bars=260`、
`max_gross_exposure=0.98`、`commission_rate=0.00025`、印花税 2023-08-28 起 5bp。

### 2.2 用法

```bash
python -m backtest list
python -m backtest validate strategies/models/atr_trend.py
python -m backtest run strategies/models/atr_trend.py \
    --start 2024-01-01 --end 2026-10-28 --universe explicit --codes 600519
```

```python
from backtest import BacktestRunner, BacktestSettings

settings = BacktestSettings.from_dict({
    "start": "2024-01-01", "end": "2026-10-28",
    "fill": "next_open", "rebalance": "weekly",
})
runner = BacktestRunner(settings)
result = runner.run("strategies/models/atr_trend.py")
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **横截面不能绑止损** | 入场在 `after_exec`，PyBroker 的 `stop_loss` 无法绑定 entry；风控退出写在 `on_bar` + `close_position()` |
| **横截面不要在 `on_bar` 买** | `on_bar` 只对**持仓**调用；开仓统一由 `after_exec` 做 |
| **非调仓日只做 buffer 退出** | `rank > top_n + buffer` 才卖，不重置目标权重 |
| **调仓日先卖后买** | 卖出回款计入买入预算（已用契约实验验证） |
| **公司行为是近似** | `corporate_actions="approx"` 由 `adj_factor` 推导，不是真实分红 |
| **ST/新股是近似** | 只有当前 ST 状态和上市日，**历史 ST 未接** |
| **`reference` 未打通** | `runner.py` 调 `build_panel` 时没传 `reference`，**带 `neutralize` 的 composite 因子在回测里会抛 `FactorError`**；需先补 reference 数据源 |
| **PyBroker 版本敏感** | 契约测试锁 2.0.2；升级要先跑 `tests/test_pybroker_contract.py` |
| **warmup 要给够** | `warmup_min_bars=260` 是下限，长窗因子（如 1320 日）需要更多前置数据，否则开头全 NaN |
