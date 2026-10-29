# 08 · strategies（策略基类与运行时上下文）

## 1. 架构与依赖

### 1.1 职责边界

`strategies` 定义**策略怎么写**：因子声明、股票池、调仓节奏、权重分配、下单意图。
它**不含撮合逻辑**（→ `backtest`），只描述"想做什么"。

策略文件约定：模块级定义 `STRATEGY`（类或工厂），loader 每次 `run` 实例化一次。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `factors` | 用 `F.*` 声明因子（唯一依赖） |
| 下游 | `backtest` | `BacktestRunner` 加载并执行 `STRATEGY` |
| 下游 | `position`（可选） | 实盘持仓同步 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `base.py` | `BaseStrategy` / `SingleSymbolStrategy` / `CrossSectionStrategy` |
| `context.py` | `StrategyContext`（下单 API）、`PanelView`（因子视图）、`OrderIntent` |
| `universe.py` | `UniverseSpec`（explicit/all_a/index）、`Rebalance` |
| `weighting.py` | `EqualWeight` / `ScoreWeight` / `InverseVolWeight` / `CappedWeight` |
| `models/*.py` | 示例策略：`atr_trend.py`、`rank_mom20.py` |

### 1.4 数据流

```
load STRATEGY
   ├─ strategy.factors()      → 需要哪些因子
   ├─ 回测预取 + build_panel
   ├─ 每根 bar：
   │    single: on_bar(symbol, ctx)
   │    cross : rank() → 选股 → weights() → 调仓意图
   └─ 订单交给引擎撮合
```

## 2. 核心类与方法

### 2.1 类表

| 类 | 关键成员 | 说明 |
|---|---|---|
| `BaseStrategy` | `name` / `mode` / `params`；`factors()` / `hyperparams()` / `validate()` | 抽象基类 |
| `SingleSymbolStrategy` | `mode="single"`；`on_bar(symbol, ctx)` | 单标的逐 bar 交易 |
| `CrossSectionStrategy` | `mode="cross"`；`rank()` / `weights()` / `on_bar()` | 横截面选股 |
| `StrategyContext` | `target_weight` / `buy_shares` / `sell_shares` / `sell_percent` / `close_position` / `set_stop` / `f(name)` | 下单 API |
| `PanelView` | `at(dt)` / `get(symbol)` | 因子视图 |
| `UniverseSpec` | `explicit(codes)` / `all_a()` / `index(code)` / `fingerprint()` | 股票池 |
| `Rebalance` | 枚举 | 调仓频率（`weekly` 等） |
| `Weighting` 族 | `EqualWeight` / `ScoreWeight` / `InverseVolWeight` / `CappedWeight` | 权重方案 |

### 2.2 用法

```python
from factors import F
from strategies.base import CrossSectionStrategy

class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    params = {"top_n": 20, "buffer": 5}

    def factors(self):
        return [F.mom(20), F.vol(20)]

    def rank(self, panel, dt):
        """panel: 因子面板视图；返回 (scores, selected)。"""
        scores = panel.at(dt)["mom20"].dropna()
        selected = scores.sort_values(ascending=False).head(self.params["top_n"]).index.tolist()
        return scores, selected

    def weights(self, panel, scores, selected):
        n = len(selected)
        return {code: 1.0 / n for code in selected} if n else {}

STRATEGY = RankMom20      # loader 找的就是这个变量
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **`rank()` 返回什么** | 排序后的代码列表；**不要在下单**，开仓由框架统一执行 |
| **`on_bar` 在横截面只对持仓调用** | 未持仓标的不会进来，所以"在 on_bar 里买"是无效的 |
| **权重和要 ≤ 1** | 超配会被 `max_gross_exposure` 截断，实际仓位与预期不符 |
| **`params` 会与传入合并** | 类属性 `params` 是默认值，实例化时 `dict(params)` 覆盖；别在运行期改类属性 |
| **`validate()` 在构造时调用** | 缺 `name` 或 `mode` 不合法会直接抛错，不会静默 |
| **因子列表要与回测预取一致** | `factors()` 返回的定义会进 `build_panel`；动态 `F.ts()` 记得注册 |
| **示例策略是代码也是测试** | `strategies/models/*.py` 被契约测试引用，改签名要一起改测试 |
