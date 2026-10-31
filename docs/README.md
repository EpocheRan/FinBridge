# FinBridge 文档总索引

> 面向 **AI 与人类读者**。约定：
> - `modules/` 只描述**当前实现**，每篇固定两节：**架构与依赖**、**核心类/方法**；
> - `design/` 记录**设计意图与取舍**，不保证逐行对应现状；
> - `guides/` 是可直接照做的用法与运维约定；
> - `notes/` 是按日期归档的变更记录、一次性迁移与回滚 SQL。

## 一、目录结构

| 目录 | 放什么 | 不放什么 |
|---|---|---|
| `modules/` | 11 个模块的现状文档 | 设计争论、历史方案 |
| `design/` | 架构、因子库、物化、报告 v2、回测架构、数据管线 | 逐步操作教程 |
| `guides/` | 回测用法、选股用法、`output/` 目录约定 | 模块 API 全量清单 |
| `data/` | 数据覆盖度报告（按日期） | 取数代码 |
| `notes/` | 变更备注、一次性迁移、回滚 SQL | 长期维护的说明 |
| `map/` | 交互式系统与数据模型图谱（`index.html` 为 GitHub Pages 入口） | 手工维护的表格 |

## 二、系统全景

FinBridge 是 A 股量化研究平台，**QuestDB 是唯一存储**，所有模块围绕它读写。

```
                 ┌──────────────────────────────────────────────┐
                 │            acquisition（采集编排）            │
                 │  orchestrator / build_orchestrator / syncers  │
                 └───────────────────┬──────────────────────────┘
                                     │ 写入
                                     ▼
                 ┌──────────────────────────────────────────────┐
                 │      QuestDB（ohlcv_1d / fin_* / moneyflow /  │
                 │       margin_detail / top_list / block_trade /│
                 │       concept_* / factor_value_* ...）        │
                 └───────────────────┬──────────────────────────┘
                                     │ 读取
                                     ▼
                 ┌──────────────────────────────────────────────┐
                 │        data_interface（数据访问层）           │
                 │ market / financial / moneyflow / sentiment /  │
                 │ concept / disclosure / api（工具封装）        │
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

## 三、模块索引

| # | 模块 | 一句话职责 | 文档 |
|---|---|---|---|
| 01 | `acquisition` | 数据采集编排：按数据集增量同步、断点续跑、构建调度 | [01-acquisition.md](modules/01-acquisition.md) |
| 02 | `data_interface` | 统一数据访问：行情/财务/资金流/情绪/板块/披露 + 工具封装 | [02-data-interface.md](modules/02-data-interface.md) |
| 03 | `factors` | 纯计算层：因子定义（DAG）、算子、横截面变换、面板构建 | [03-factors.md](modules/03-factors.md) |
| 04 | `factor_engine` | 因子物化层：注册表、批量计算、长表落库、审计 | [04-factor-engine.md](modules/04-factor-engine.md) |
| 05 | `research` | 因子研究报告：IC/分层/评级/可视化/评分卡入库 | [05-research.md](modules/05-research.md) |
| 06 | `screener` | 选股：快照构建、条件筛选、覆盖率与断点 | [06-screener.md](modules/06-screener.md) |
| 07 | `backtest` | 回测：PyBroker 引擎 + A 股规则 + 横截面执行 | [07-backtest.md](modules/07-backtest.md) |
| 08 | `strategies` | 策略基类：单标的/横截面、股票池、权重、上下文 | [08-strategies.md](modules/08-strategies.md) |
| 09 | `position` | 持仓管理：组合/持仓/交易记录、成本与风控模式 | [09-position.md](modules/09-position.md) |
| 10 | `scripts` | 运维脚本：回填、修复、报告、评分卡、方向校准 | [10-scripts.md](modules/10-scripts.md) |
| 11 | `MCPserver` | MCP 工具服务器：注册各模块工具、传输与生命周期 | [11-mcp-server.md](modules/11-mcp-server.md) |

## 四、文档索引

### 4.1 设计

| 文档 | 内容 |
|---|---|
| [design/architecture_v2.md](design/architecture_v2.md) | 模块边界、DB 所有权、存储分层与里程碑 |
| [design/factor_library_v1.md](design/factor_library_v1.md) | 因子分类、命名、版本与增量方式 |
| [design/factor_materialization_v1.md](design/factor_materialization_v1.md) | 因子物化：registry/长表/latest/缓存 |
| [design/factor_report_v2.md](design/factor_report_v2.md) | 因子报告 v2：方向、价差、成本、断言、归因 |
| [design/backtest_architecture.md](design/backtest_architecture.md) | 回测引擎详细架构（PyBroker + 自建账本） |
| [design/data_pipeline_v1.md](design/data_pipeline_v1.md) | 分时/板块映射/披露三条 P0–P2 数据链路 |

### 4.2 指南

| 文档 | 内容 |
|---|---|
| [guides/backtest.md](guides/backtest.md) | 回测安装、CLI、策略/因子写法与当前限制 |
| [guides/screener.md](guides/screener.md) | 选股四层结构、表、MCP 与 CLI 用法 |
| [guides/output_layout.md](guides/output_layout.md) | `output/` 目录约定（交付物 / 日志 / 回测产物） |
| [../analysis/README.md](../analysis/README.md) | 事件研究、覆盖度与数据库体检脚本（仓库根 `analysis/`） |

### 4.3 数据与记录

| 文档 | 内容 |
|---|---|
| [data/coverage.md](data/coverage.md) | `ohlcv_1d` 与结构数据的实际覆盖区间、可研究起点 |
| [notes/20261031_factor_report_v2.md](notes/20261031_factor_report_v2.md) | 因子报告 v2 的全库实测与变更备注 |
| [notes/20261030_drop_retired_tables.sql](notes/20261030_drop_retired_tables.sql) | 已删除的 5 张退役表回滚 SQL |

### 4.4 图谱

| 文档 | 内容 |
|---|---|
| [map/finbridge_system_map.html](map/finbridge_system_map.html) | 交互图谱：模块职责/核心函数 + 数据域、表结构与逻辑关系 |

## 五、依赖规则（实测 import 关系）

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
4. 任何模块写 `output/` 时必须遵守 [guides/output_layout.md](guides/output_layout.md) 的目录约定。

## 六、AI 阅读顺序建议

| 目标 | 建议顺序 |
|---|---|
| 改因子计算 | 03-factors → 04-factor-engine → 05-research |
| 加数据源 | 02-data-interface → 01-acquisition |
| 改回测 | 08-strategies → 07-backtest → design/backtest_architecture |
| 改选股 | 06-screener → 04-factor-engine |
| 接新工具 | 11-mcp-server → 对应模块的 `api.py` |

## 七、通用注意事项

| 主题 | 结论 |
|---|---|
| 时区 | QuestDB 存的北京时间**标签为 UTC**；读出后 `.dt.tz_localize(None)` 剥掉，**不要转换** |
| WAL | 写入后立即读可能读不到，需要重试（`concept/repository.py` 有现成实现） |
| 去重 | 所有长表用 `DEDUP UPSERT KEYS`；QuestDB 不支持 `DELETE` |
| 行情 schema | `market` 存交易所（`SSE/SZSE/BSE`），`type` 存品种（`stock/etf/lof/fund/idx`）；OHLC 的 DEDUP 含 `type` |
| 大表 | `factor_value_daily` 已千万行级，查询必须带 `ts` 范围或 `factor_id` |
| 内存 | 批量写入要分批（`service.build` 每 5 万行冲刷），32G 机器同时只跑一个重任务 |
| 命名 | 因子 ID 统一 `category.semantic[_window]`，小写；见 `factors/naming.py` |

## 八、维护约定

- **模块文档**：实现变更时同步更新对应 `modules/NN-*.md`，不保留过时签名；
- **设计文档**：记录“为什么这么做”，实现细节变化时不强行回写；
- **指南**：命令必须可在当前代码上直接执行；
- **变更记录**：放 `notes/`，文件名以 `YYYYMMDD_` 开头；
- **图谱**：`map/finbridge_system_map.html` 按当前模块与表结构维护，不包含本地数据行数；模块入口或表结构变化后同步更新。
