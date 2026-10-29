# 06 · screener（选股）

## 1. 架构与依赖

### 1.1 职责边界

`screener` 负责"**给定条件，产出当日候选池**"。典型链路：构建快照（universe + 因子覆盖）→
执行筛选（SQL 条件 + 排序）→ 返回带指标的结果。

采用 DDD 四层：`domain`（规则）/ `application`（用例）/ `infrastructure`（实现）/ `interface`（CLI/API）。
快照与增量构建由 `acquisition.BuildOrchestrator` 承担，`screener` 只做编排调用。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `factor_engine` | 因子物化与读取（`FactorStorePort` 适配） |
| 上游 | `data_interface` | 行情、交易日历、停牌 |
| 上游 | `acquisition.BuildOrchestrator` | **硬依赖**：快照构建、作业调度 |
| 下游 | `MCPserver`（`screener.interface.api`） | 工具 |
| 下游 | `research`/`backtest` | 复用其快照做研究/回测股票池 |

### 1.3 关键文件

| 层 | 文件 | 作用 |
|---|---|---|
| 入口 | `bootstrap.py` | `ScreenerApplication`（start/stop）、`build_screener_application()` |
| | `config.py` | `ScreenerSettings` / `BackfillSettings` / `WorkerSettings` / `QuerySettings` |
| application | `screener_service.py` | `screen(spec)` / `rebuild` / `status` / `get_metrics` |
| | `factor_service.py` | `compute_chunk` / `coverage` |
| | `backfill_service.py` | `ensure_ohlc` / `ensure_daily` / `ensure_baseline_data` |
| | `ports.py` | 端口协议（`MarketDataPort` / `FactorStorePort` / `BuildStatePort` …） |
| domain | `models.py` / `fields.py` / `factors.py` / `coverage.py` / `suspension.py` / `baseline.py` | 规则与数据结构 |
| infrastructure | `repository.py` / `query_builder.py` / `job_queue.py` / `mappers.py` | SQL 生成、任务队列、映射 |
| interface | `api.py` / `cli.py` / `worker_main.py` | MCP 工具、CLI、worker 进程 |

### 1.4 数据流

```
screen(ScreenSpec)
   ├─ 解析 universe（explicit / all_a / index）
   ├─ ensure 快照：BuildOrchestrator.ensure_snapshot / ensure_job
   │     └─ 缺失因子 → factor_engine.build（STORED）
   ├─ query_builder 生成 SQL（过滤 + 排序 + 限流）
   ├─ 过滤停牌/ST/新股、校验覆盖率
   └─ 返回 [{code, score, metrics...}]
```

## 2. 核心类与方法

### 2.1 类表

| 类/函数 | 关键方法 | 说明 |
|---|---|---|
| `ScreenerApplication` | `start()` / `stop()` | 生命周期：起 worker、连 DB |
| `build_screener_application()` | — | 工厂，返回已装配应用 |
| `ScreenerService` | `screen(spec)` / `rebuild(request)` / `status(build_id)` / `get_metrics(codes, start, end)` | 核心用例 |
| `FactorService` | `compute_chunk(codes, start, end)` / `coverage(codes, start, end)` | 分块算因子、覆盖率 |
| `BackfillService` | `ensure_ohlc` / `ensure_daily` / `ensure_baseline_data` / `mark_no_data` | 数据补齐 |
| `ScreenSpec`（domain） | 字段：universe、filters、order_by、limit、as_of | 筛选规格 |

### 2.2 用法

```python
from screener.bootstrap import build_screener_application
from screener.domain.models import Filter, ScreenSpec, Sort, UniverseSpec

app = build_screener_application()
app.start()
svc = app.screener            # ScreenerService

spec = ScreenSpec(
    as_of="2026-10-28",
    universe=UniverseSpec(market="MLCN", codes=["600519", "000001"], exclude_st=True),
    filters=[Filter(field="pe_ttm", op="<", value=30)],
    sort=[Sort(field="momentum.ret_21d", desc=True)],
    limit=50,
)
result = svc.screen(spec)     # ScreenResult(rows/coverage/quality/status/build_id/warnings)
app.stop()
```

CLI：`python -m screener --help`；worker：`python -m screener.interface.worker_main`。

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **快照语义** | `as_of` 是快照时点，不是"最新数据"；用未来数据会引入幸存者偏差 |
| **覆盖率门槛** | 因子覆盖不足时结果会被截断或标 `INSUFFICIENT`，不要当成"没有候选" |
| **停牌/ST 过滤** | 默认排除；`suspension.py` 按交易日判定，别自己写日期比较 |
| **worker 必须启动** | 异步 rebuild 依赖 `job_queue`；只起 API 不起 worker 会一直 `PENDING` |
| **对 `BuildOrchestrator` 是硬依赖** | 改它的签名要同步改 `screener/{bootstrap,application,infrastructure}` 四处 |
| **STORED 因子缺失会报错** | 选股不会现算 STORED 因子；先跑 `factor_engine.build` |
| **SQL 注入** | 字段名必须走 `fields.py` 白名单，不要拼用户输入 |
