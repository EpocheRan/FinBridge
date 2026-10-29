# FinBridge 文档总索引

> 面向 **AI 与人类读者**：每个模块一篇文档，固定两节 ——
> **1. 架构与依赖**（职责边界、上下游、关键文件、数据流）
> **2. 核心类/方法**（签名、用法、注意事项与坑）。
>
> 约定：文档只描述**当前实现**；设计意图见 `docs/design/`，回测细节见 `docs/backtest/`。

---

## 一、系统全景

FinBridge 是 A 股量化研究平台，**QuestDB 是唯一存储**，所有模块围绕它读写。

```
                 ┌──────────────────────────────────────────────┐
                 │            acquisition（采集编排）            │
                 │  orchestrator / build_orchestrator / syncers  │
                 └───────────────────┬──────────────────────────┘
                                     │ 写入
                                     ▼
                 ┌──────────────────────────────────────────────┐
                 │      QuestDB（ohlcv_1d / fin_* / flow /       │
                 │       factor_value_daily / dim_board ...）    │
                 └───────────────────┬──────────────────────────┘
                                     │ 读取
                                     ▼
                 ┌──────────────────────────────────────────────┐
                 │        data_interface（数据访问层）           │
                 │ market / financial / moneyflow / concept /    │
                 │ disclosure / api（工具封装）                   │
                 └───────────────────┬──────────────────────────┘
                                     │
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │     factors      │   │   factor_engine   │   │     position     │
   │ 声明式因子/面板   │◄──│  物化/注册/服务    │   │   持仓与交易台账  │
   └────────┬─────────┘   └────────┬─────────┘   └──────────────────┘
            │                      │
            ▼                      ▼
   ┌────────────────────────────────────────────────────────────┐
   │  strategies（策略）→ backtest（回测）                        │
   │  screener（选股） / research（因子研究报告 + 评分卡）        │
   └───────────────────────────┬────────────────────────────────┘
                               ▼
                    MCPserver（对外工具接口）
```

## 二、模块索引

| # | 模块 | 一句话职责 | 文档 |
|---|---|---|---|
| 01 | `acquisition` | 数据采集编排：按数据集增量同步、断点续跑、构建调度 | [01-acquisition.md](modules/01-acquisition.md) |
| 02 | `data_interface` | 统一数据访问：行情/财务/资金流/板块/披露 + 工具封装 | [02-data-interface.md](modules/02-data-interface.md) |
| 03 | `factors` | 纯计算层：因子定义（DAG）、算子、横截面变换、面板构建 | [03-factors.md](modules/03-factors.md) |
| 04 | `factor_engine` | 因子物化层：注册表、批量计算、长表落库、审计 | [04-factor-engine.md](modules/04-factor-engine.md) |
| 05 | `research` | 因子研究报告：IC/分层/评级/可视化/评分卡入库 | [05-research.md](modules/05-research.md) |
| 06 | `screener` | 选股：快照构建、条件筛选、覆盖率与断点 | [06-screener.md](modules/06-screener.md) |
| 07 | `backtest` | 回测：PyBroker 引擎 + A 股规则 + 横截面执行 | [07-backtest.md](modules/07-backtest.md) |
| 08 | `strategies` | 策略基类：单标的/横截面、股票池、权重、上下文 | [08-strategies.md](modules/08-strategies.md) |
| 09 | `position` | 持仓管理：组合/持仓/交易记录、成本与风控模式 | [09-position.md](modules/09-position.md) |
| 10 | `scripts` | 运维脚本：回填、修复、报告、评分卡、方向校准 | [10-scripts.md](modules/10-scripts.md) |
| 11 | `MCPserver` | MCP 工具服务器：注册各模块工具、传输与生命周期 | [11-mcp-server.md](modules/11-mcp-server.md) |

补充文档：

| 文档 | 内容 |
|---|---|
| [design/architecture_v2.md](design/architecture_v2.md) | 架构演进与设计决策 |
| [design/factor_library_v1.md](design/factor_library_v1.md) | 因子库设计（分类、命名、版本） |
| [design/factor_materialization_v1.md](design/factor_materialization_v1.md) | 因子物化设计 |
| [backtest/ARCHITECTURE.md](backtest/ARCHITECTURE.md) | 回测引擎详细架构 |
| [backtest/README.md](backtest/README.md) | 回测用法与现状 |
| [data/p0_p2_data_pipeline.md](data/p0_p2_data_pipeline.md) | 分时/板块/披露三条数据链路 |
| [screener/README.md](screener/README.md) | 选股模块用法 |

## 三、依赖规则（实测的 import 关系）

```
factors          -> （无内部依赖，纯计算）★
strategies       -> factors
data_interface   -> （无硬依赖；对 acquisition/position 是函数内延迟导入）
factor_engine    -> factors, data_interface
position         -> factors, data_interface
screener         -> factors, factor_engine, data_interface, acquisition
backtest         -> factors, factor_engine, data_interface, strategies
research         -> data_interface（仅 common.config/timeutil）
acquisition      -> data_interface, factor_engine, screener
MCPserver        -> 全部（注册工具）
```

硬约束：

1. **`factors/` 不得 import 任何其他业务模块**——它必须能在无 DB、无网络的环境下单测；
2. `data_interface` 对 `acquisition` / `position` 的引用必须留在**函数体内**（`api/runtime.py`），
   否则会形成循环依赖；
3. `screener` 对 `acquisition.BuildOrchestrator` 是**硬依赖**，改这个类要同步改 screener；
4. 任何模块写 `output/` 时必须遵守 [output_layout.md](output_layout.md) 的目录约定。

## 四、AI 阅读顺序建议

| 目标 | 建议顺序 |
|---|---|
| 改因子计算 | 03-factors → 04-factor-engine → 05-research |
| 加数据源 | 02-data-interface → 01-acquisition |
| 改回测 | 08-strategies → 07-backtest |
| 改选股 | 06-screener → 04-factor-engine |
| 接新工具 | 11-mcp-server → 对应模块的 `api.py` |

## 五、通用注意事项

| 主题 | 结论 |
|---|---|
| 时区 | QuestDB 存的北京时间**标签为 UTC**；读出后 `.dt.tz_localize(None)` 剥掉，**不要转换** |
| WAL | 写入后立即读可能读不到，需要重试（`concept/repository.py` 有现成实现） |
| 去重 | 所有长表用 `DEDUP UPSERT KEYS`；QuestDB 不支持 `DELETE` |
| 大表 | `factor_value_daily` 已千万行级，查询必须带 `ts` 范围或 `factor_id` |
| 内存 | 批量写入要分批（`service.build` 每 5 万行冲刷），32G 机器同时只跑一个重任务 |
| 命名 | 因子 ID 统一 `category.semantic[_window]`，小写；见 `factors/naming.py` |
