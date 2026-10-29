# 09 · position（持仓与交易台账）

## 1. 架构与依赖

### 1.1 职责边界

`position` 管理**真实/模拟持仓**：组合、持仓、交易记录，以及基于行情的成本、盈亏、止损位刷新。
它不做撮合（成交由用户/回测产生），只维护台账与风控建议。

核心概念：`Portfolio`（组合）→ `Position`（持仓）→ `TradeRecord`（逐笔交易）。
每个持仓绑定一个**模式**（`ATR` / `GRID`），模式决定止损、加仓、目标位的计算方式。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `data_interface` | 行情（刷新市值/浮动盈亏） |
| 上游 | `factors` | ATR 等指标（`ATRMode`） |
| 下游 | `MCPserver` | `position.llm_tool` 暴露给 LLM 的工具 |
| 下游 | `data_interface.api.runtime` | `get_position_interface()` 单例 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `manager.py` | `PositionManager`：全部 CRUD + 计算 + 刷新；`compute_ledger` |
| `schema.py` | SQLAlchemy 模型：`PortfolioModel` / `PositionModel` / `TradeRecordModel`、`create_tables` |
| `modes/__init__.py` | `MODE_REGISTRY`、`create_mode(name, params)`、`DEFAULT_MODE="ATR"` |
| `modes/base.py` | `PositionModeBase`：模式接口（止损/加仓/信号） |
| `modes/atr_mode.py` | `ATRMode`：ATR 止损与仓位 |
| `modes/grid_mode.py` | `GridMode`：网格加仓/减仓 |
| `llm_tool.py` | `PositionAgentInterface`：面向 LLM 的高层封装 |

### 1.4 数据流

```
add_trade_record(pos_id, buy/sell, qty, price)
   ├─ 更新 position.quantity / cost
   ├─ 写 trade_record（历史可追溯）
   └─ calculate_profit → 浮盈/已实现盈亏

refresh_all_positions(portfolio_id)
   ├─ 取最新价（data_interface）
   ├─ 重算市值/浮盈/止损位（按 mode）
   └─ 写回 position
```

## 2. 核心类与方法

### 2.1 类表

| 类 | 关键方法 | 说明 |
|---|---|---|
| `PositionManager` | `create_profolio` / `modify_profolio` / `list__profolio` / `delete_profolio` | 组合 CRUD |
| | `create_position` / `modify_position` / `delete_position` / `list_positions` | 持仓 CRUD |
| | `add_trade_record` / `modify_trade_record` / `delete_trade_record` / `list_trade_record` | 交易记录 |
| | `calculate_profolio` / `calculate_profit` / `refresh_all_positions` / `get_portfolio_summary` | 计算与刷新 |
| | `save_stop_price` / `adjust_quantity` | 止损位、仓位调整 |
| `PositionAgentInterface` | 高层工具方法 | 给 LLM 用 |
| `ATRMode` / `GridMode` | 模式实现 | 止损/加仓策略 |

> **API 命名有历史拼写**：`create_profolio` / `list__profolio`（注意双下划线与 `profolio`）。
> 这是既有对外接口，**不要改名**，否则破坏 MCP 工具与调用方。

### 2.2 用法

```python
from position.manager import PositionManager

pm = PositionManager()
pf = pm.create_profolio("主账户", cash=100_000)
pos = pm.create_position(pf.id, "600519", "贵州茅台", mode="ATR", mode_params={"atr_mult": 2})
pm.add_trade_record(pos.id, trade_type="BUY", quantity=100, price=1500.0, trade_date="2026-10-28")
pm.refresh_all_positions(pf.id)
print(pm.get_portfolio_summary(pf.id))
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **拼写不要"修正"** | `create_profolio` / `list__profolio` 是既有 API，改名会破坏 MCP 与外部脚本 |
| **模式名大小写** | `create_mode` 内部 `upper()`；传 `"atr"` 可以，但 `MODE_REGISTRY` 的键是大写 |
| **交易记录是真相来源** | 持仓数量由 trade 汇总推导；直接改 `quantity` 会导致台账对不上 |
| **删除持仓需要 history_id** | `delete_position(pos_id, history_id)` 是软删除语义，别只传 pos_id |
| **刷新依赖行情可用性** | 停牌/退市时最新价缺失，浮盈会保留旧值而不是归零 |
| **成本口径** | 买入含费用、卖出含印花税；`calculate_profit` 的 `recalculate=True` 才会重算历史 |
| **并发** | 没有行锁；多进程同时改同一持仓会覆盖，LLM 工具应在单进程内调用 |
