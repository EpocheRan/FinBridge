# 01 · acquisition（数据采集编排）

## 1. 架构与依赖

### 1.1 职责边界

`acquisition` 是**唯一负责"把外部数据写进 QuestDB"的编排模块**。它不解析业务语义、不算因子，
只做三件事：

1. **增量同步**：按数据集判断缺哪些日期/代码，只补齐缺失部分；
2. **断点续跑**：游标落库（`scheduled_task_state`），中断后从上次位置继续；
3. **构建调度**：为选股/回测准备"快照"（universe + 覆盖率），串行化并发构建请求。

对外**唯一入口**是 `python -m acquisition run` / MCP `acquisition_run`，两者都走
`Orchestrator` + `REAL_SYNCERS`。`AShareSyncService` 只提供各数据集的 `sync_*` 方法，
不再承担总调度（旧的 `run(dataset=...)` 与 `scheduled_sync_a_shares` 已删除）。

它**不**负责：数据读取（→ `data_interface`）、因子计算（→ `factor_engine`）、选股逻辑（→ `screener`）。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | 外部数据源 | Tushare / BaoStock / 东财 / 同花顺 / 腾讯 / 新浪 |
| 下游 | `data_interface.market/financial/moneyflow/sentiment/concept` | 调用其 `sync_*` 方法写库 |
| 下游 | `screener` | 通过 `BuildOrchestrator` 触发快照构建 |
| 下游 | `factor_engine` | `syncers.factors` / `syncers.screener` 触发因子物化 |
| 被调用 | `MCPserver`（`acquisition.api`）、`data_interface.api.runtime`（延迟导入） | 工具/单例 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `orchestrator.py` | 对外编排：`Scope`、`DATASET_ORDER`、`Orchestrator.run()` |
| `syncers.py` | `REAL_SYNCERS`：数据集名 → 具体同步函数 |
| `datasets/service.py` | `AShareSyncService`：各数据集 `sync_*` 方法的组合 |
| `datasets/*.py` | 数据集实现（daily/benchmark/financial/valuation/moneyflow/sentiment/screener/concept） |
| `datasets/base.py` | 状态键、代码/日期归一、`TASK_NAME` |
| `build_orchestrator.py` | 快照/作业构建：`ensure_snapshot` / `ensure_job` |
| `state.py` | `TaskStateRepository`：`scheduled_task_state` 读写 |
| `cli.py` / `__main__.py` | `python -m acquisition run/update/status` |
| `api.py` | MCP `acquisition_run` / `acquisition_status`（唯一同步工具） |

### 1.4 数据流

```
Scope(datasets, start, end, codes, boards)
        │
        ▼
Orchestrator.run(scope)                  # 按 DATASET_ORDER 串行
        │
        ├─ REAL_SYNCERS[name](scope)     # 适配到 AShareSyncService.sync_*
        ├─ data_interface.sync_*         # 取数 / 归一 / 复权 / PIT
        ├─ QuestDB（WAL + DEDUP）
        └─ scheduled_task_state          # cursor / pending / status
```

### 1.5 数据集与写入表

| # | 数据集 | 写入表 | 说明 |
|---|---|---|---|
| 1 | `trading_calendar` | `trading_days` | 读路径按需补齐日历 |
| 2 | `stock_basic` | `stock_basic_info` | A 股 L/P/D 状态 |
| 3 | `ohlcv_1d` | `ohlcv_1d` | 股票 + ETF/LOF 日线、`adj_factor` |
| 4 | `benchmark` | `ohlcv_1d` | 基准指数（默认沪深 300） |
| 5 | `financial` | `fin_report_meta`、三张报表、`fin_indicator` | 按股票池分批 |
| 6 | `valuation` | `fin_valuation_daily` | 按交易日全市场 |
| 7 | `moneyflow` | `moneyflow_daily`、`moneyflow_source` | 四档资金流 |
| 8 | `margin_detail` | `margin_detail` | 两融余额/买入/偿还 |
| 9 | `top_list` | `top_list` | 龙虎榜 |
| 10 | `block_trade` | `block_trade` | 大宗交易 |
| 11 | `concept_meta` | `concept_board_dim` | 板块目录 |
| 12 | `concept_daily` | `concept_board_daily` | 板块日线 |
| 13 | `concept_member` | `concept_member_snapshot` | 成分快照 |
| 14 | `concept_class` | `concept_classification` | 概念分类树 |
| 15 | `factors` | `factor_value_daily` / `factor_value_latest` | 调 `factor_engine` |
| 16 | `screener` | `factor_daily_wide` | 调 `BuildOrchestrator` |

## 2. 核心类与方法

### 2.1 类表

| 类/函数 | 关键方法 | 说明 |
|---|---|---|
| `Scope` | 数据类 | 同步范围：`start/end/codes/boards/datasets/factor_ids` |
| `Orchestrator` | `run(scope)` | **对外主入口**：按顺序执行、续跑、收集错误 |
| `REAL_SYNCERS` | 数据集函数表 | 数据集名 → `sync_daily` / `sync_margin_detail` … |
| `AShareSyncService` | `sync_daily` / `sync_financials` / `sync_benchmark` / `sync_screener` / `sync_moneyflow` / `sync_margin_detail` / `sync_top_list` / `sync_block_trade` / `sync_concept_*` / `sync_valuation` / `status` | 各数据集的有界批次实现 |
| `BuildOrchestrator` | `ensure_snapshot` / `ensure_job` / `ensure_baseline_snapshot` / `recover` | 快照与作业调度，带互斥锁 |
| `TaskStateRepository` | `load/save` | 断点状态持久化 |
| `build_orchestrator()` | — | 工厂，返回已注入 `REAL_SYNCERS` 的编排器 |

### 2.2 用法

```python
from datetime import date
from acquisition import Scope, build_orchestrator

scope = Scope(
    start=date(2026, 10, 1),
    end=date(2026, 10, 30),
    datasets=("ohlcv_1d", "margin_detail", "top_list", "block_trade"),
)
result = build_orchestrator().run(scope)
print(result["results"], result["errors"])
```

```python
# 只跑单个数据集（仍是同一个入口）
from acquisition.syncers import REAL_SYNCERS
REAL_SYNCERS["margin_detail"](scope)
```

CLI：

```bash
python -m acquisition run                                  # 全数据集
python -m acquisition run --datasets ohlcv_1d,margin_detail,top_list,block_trade
python -m acquisition run --start 2026-01-01 --no-financial
python -m acquisition status
```

MCP：

```python
acquisition_run(datasets="margin_detail,top_list,block_trade",
                start_date="2026-10-01", end_date="2026-10-30")
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **dataset 白名单** | 只能是上表 16 个之一，拼错直接抛 `ValueError` |
| **`start_date` 语义随数据集变** | 日线/基准/情绪/概念是交易日；财务是报告年份 + 估值日期范围 |
| **单次是分批的** | 日线 20 个交易日、财务 15 只、估值 5 日、分档 20 日、情绪 10 日、板块日线 50 个；要 `--loop` 或反复心跳才 `done` |
| **financial 会被自动跳过** | CLI 发现 `--start` 年份与库内游标不一致时会跳过，除非 `--force-financial` |
| **情绪类首次从最新日期续跑** | `margin_detail/top_list/block_trade` 无游标时从库内 `max(ts)` 开始，避免把默认 2020 变成全历史回填；历史用 `scripts/backfill_history.py --phase p2-structure` |
| **情绪空日只重试 3 天** | 龙虎榜/大宗可能当天没有数据；只对最近 3 天记 `pending`，避免历史空日永久挂起 |
| **`force=True` 会重跑** | 跳过"已有数据"判断，慎用（几小时级） |
| **构建互斥** | `BuildOrchestrator` 用锁串行化；并发请求会排队而不是并行，长任务会阻塞其他构建 |
| **WAL 可见性** | 写完立刻读可能读不到，读取侧要重试（见 `concept/repository.py`） |
| **不要在这里算因子** | 因子属于 `factor_engine`，混进来会破坏分层 |
| **异常吞掉** | 单个代码失败通常记 `sync_state.status=ERROR` 后继续，`status()` 才能看到失败清单 |
