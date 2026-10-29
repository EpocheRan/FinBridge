# 01 · acquisition（数据采集编排）

## 1. 架构与依赖

### 1.1 职责边界

`acquisition` 是**唯一负责"把外部数据写进 QuestDB"的模块**。它不解析业务语义、算因子，
只做三件事：

1. **增量同步**：按数据集判断缺哪些日期/代码，只补齐缺失部分；
2. **断点续跑**：游标落库（`sync_state`），中断后从上次位置继续；
3. **构建调度**：为选股/回测准备"快照"（universe + 覆盖率），串行化并发构建请求。

它**不**负责：数据读取（→ `data_interface`）、因子计算（→ `factor_engine`）、选股逻辑（→ `screener`）。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | 外部数据源 | Tushare / BaoStock / 东财 / 同花顺 / 腾讯 / 新浪 |
| 下游 | `data_interface.market/financial/moneyflow/concept` | 调用其 `sync_*` 方法写库 |
| 下游 | `screener` | 通过 `BuildOrchestrator` 触发快照构建 |
| 下游 | `factor_engine` | `syncers.factors` / `syncers.screener` 触发因子物化 |
| 被调用 | `MCPserver`（`acquisition.api`）、`data_interface.api.runtime`（延迟导入） | 工具/单例 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `orchestrator.py` | 通用编排：`Scope`（要同步什么）+ `Orchestrator.run()` |
| `build_orchestrator.py` | 快照/作业构建：`ensure_snapshot` / `ensure_job` / `ensure_baseline_snapshot` |
| `datasets/service.py` | `AShareSyncService`：数据集分发入口（11 种 dataset） |
| `datasets/*.py` | 各数据集的 mixin（daily/benchmark/financial/valuation/screener/moneyflow/concept） |
| `datasets/base.py` | `DATASETS` 白名单、`normalize_selected_codes`、`parse_date_range` |
| `syncers.py` | 面向 MCP 的薄封装（`daily()` / `financial()` / `factors()` …） |
| `state.py` | `TaskStateRepository`：`sync_state` / `build_state` 读写 |
| `cli.py` / `__main__.py` | `python -m acquisition <dataset>` |

### 1.4 数据流

```
Scope/AShareSyncService.run(dataset=...)
        │
        ├─ 解析参数（代码、日期、游标）
        ├─ 判断缺失区间（对比 sync_state / ohlcv_1d 已有日期）
        ├─ 调用 data_interface 的 sync_*（内部按源优先级取数、归一化）
        ├─ 写 QuestDB（WAL，DEDUP UPSERT）
        └─ 更新 sync_state（cursor / last_ts / status）
```

## 2. 核心类与方法

### 2.1 类表

| 类/函数 | 关键方法 | 说明 |
|---|---|---|
| `Scope` | 数据类 | 同步范围：`start/end/codes/boards/datasets/factor_ids` |
| `Orchestrator` | `run(scope)` | 按 Scope 依次同步多个数据集 |
| `AShareSyncService` | `run(dataset=...)` / `status()` | **主入口**，11 种 dataset |
| `BuildOrchestrator` | `ensure_snapshot` / `ensure_job` / `ensure_baseline_snapshot` / `recover` | 快照与作业调度，带互斥锁 |
| `TaskStateRepository` | `load/save` | 断点状态持久化 |
| `build_orchestrator()` | — | 工厂，返回已注入依赖的 `BuildOrchestrator` |

### 2.2 用法

```python
from acquisition import AShareSyncService

svc = AShareSyncService()
# 日线：缺哪补哪（游标自动续跑）
svc.run(dataset="daily", start_date="2026-01-01", end_date="2026-10-28")
# 财务：按报告年份 + 估值日期区间
svc.run(dataset="financial", start_date="2020-01-01", end_date="2026-10-28")
# 只补指定代码
svc.run(dataset="daily", codes=["600519", "000001"], force=True)
```

```python
from acquisition.build_orchestrator import build_orchestrator

bo = build_orchestrator()
state = bo.ensure_snapshot(codes=["600519"], as_of="2026-10-28", auto_build=True)
print(state.build_id, state.coverage)
```

CLI：

```bash
python -m acquisition daily --start 2026-01-01 --end 2026-10-28
python -m acquisition all --start 2025-01-01 --include-benchmark --include-screener
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **dataset 白名单** | 只能是 `daily / benchmark / financial / valuation / screener / moneyflow / concept_meta / concept_daily / concept_member / concept_class / all`，拼错直接抛 `ValueError` |
| **`start_date` 语义随数据集变** | 日线/基准是交易日；财务是**报告年份下限** + 估值日期范围；不要用同一套假设 |
| **游标只对部分数据集生效** | `cursor` 只传给 `daily / financial / moneyflow / valuation`，其余数据集无游标 |
| **`force=True` 会重跑** | 跳过"已有数据"判断，慎用（几小时级） |
| **构建互斥** | `BuildOrchestrator` 用锁串行化；并发请求会排队而不是并行，长任务会阻塞其他构建 |
| **WAL 可见性** | 写完立刻读可能读不到，读取侧要重试（见 `concept/repository.py`） |
| **不要在这里算因子** | 因子属于 `factor_engine`，混进来会破坏分层 |
| **异常吞掉** | 单个代码失败通常记 `sync_state.status=ERROR` 后继续，`status()` 才能看到失败清单 |
