# FinBridge MCP

> 面向 AI Agent 的量化数据与回测基础设施：行情、财务、**因子筛股（Screener）**、组合持仓、整合同步通过 MCP 暴露；回测作为独立离线链路，复用同一份数据与 A 股执行规则（研究/优化为后续扩展）。

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 5 分钟快速开始](#2-5-分钟快速开始)
- [3. 配置说明](#3-配置说明)
- [4. 数据口径与存储契约](#4-数据口径与存储契约)
- [5. Screener 因子筛股](#5-screener-因子筛股)
- [6. 回测（Backtest）](#6-回测backtest)
- [7. 代码结构](#7-代码结构)
- [8. 数据库设计与迁移](#8-数据库设计与迁移)
- [9. MCP 工具清单](#9-mcp-工具清单)
- [10. 整合同步](#10-整合同步acquisition)
- [11. 常见问题](#11-常见问题)
- [12. 开发约定与测试](#12-开发约定与测试)
- [13. 因子研究报告与评分卡](#13-因子研究报告与评分卡)
- [14. 模块文档](#14-模块文档)

---

## 1. 项目定位

FinBridge 是面向 AI Agent 的量化数据与回测基础设施：

- **在线链路**：行情、财务、因子筛股、组合持仓、整合同步，统一通过 MCP 工具暴露；
- **离线链路**：复用同一份数据与 A 股执行规则，做单股/横截面回测；优化、因子研究为后续扩展；
- **数据底座**：行情表只存**不复权价 + 累计复权因子**，复权在读取时计算；所有服务共享 QuestDB 与进程内单例。

### 1.1 总体架构

```text
                 ┌──────────────────────────────────────────────┐
                 │                 MCP Server                   │
                 │ market / financial / screener / position /   │
                 │ acquisition / factor tools                    │
                 └───────────────┬──────────────────────────────┘
                                 │ data_interface.api.runtime（进程内单例）
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
┌───────────────┐       ┌────────────────┐        ┌────────────────┐
│ data_interface│       │ screener       │        │ position       │
│ 行情/财务/日历 │       │ 因子/基线/构建 │        │ 组合/持仓/交易 │
└───────┬───────┘       └───────┬────────┘        └───────┬────────┘
        └───────────────────────┼─────────────────────────┘
                                ▼
                         ┌─────────────┐
                         │   QuestDB   │
                         └──────┬──────┘
                                │ 只读
                 ┌──────────────▼──────────────┐
                 │  离线回测 / 研究（可选依赖） │
                 │ strategies → factors →      │
                 │ backtest → ledger / outputs │
                 └─────────────────────────────┘
```

### 1.2 分层与依赖

```text
interface        MCPserver.py / data_interface.api / screener.interface /
                 backtest.cli / acquisition.api
      │
application      screener.application / backtest.application / position.manager
      │
domain           screener.domain / backtest.domain / factors.library /
                 strategies.base
      ▲
      │ 实现端口
infrastructure   data_interface / screener.infrastructure /
                 backtest.infrastructure（QuestDB、第三方源、PyBroker、输出）
      │
shared           data_interface.common / scripts.schemas
```

依赖规则：

- 需要分层的模块（`screener/`、`backtest/`）遵循 `interface → application → domain`；
  `data_interface` 是共享基础设施，被各层复用；`bootstrap` 是唯一装配点；
- `strategies/`、`factors/` 是纯 Python/pandas，不 import `backtest`、PyBroker、QuestDB、`data_interface`；
- `backtest/` 单向依赖 `strategies` / `factors`，只有 `backtest/infrastructure/engine/pybroker_engine.py` import PyBroker；
- `data_interface` 不反向依赖 `screener` / `position` / `backtest`；
- `MCPserver.py` 只做工具注册与启动，不写业务逻辑，也不在启动时建表。

### 1.3 主要模块

| 模块 | 目录 | 关键内容 | 对外入口 |
|---|---|---|---|
| 数据接口 | `data_interface/` | `common/`（配置、QuestDB、交易日历、限流、代码/板块）、`market/`（loader/fetcher/adjust/adj_factors/sources）、`financial/`（service/repository/normalizer/metrics/fields/sources）、`moneyflow/`（分档 service/repository/sources）、`sentiment/`（两融/龙虎榜/大宗 service/repository）、`concept/`（板块/成分/分类 service/repository/sources）、`api/`（runtime 单例、MCP tools） | `data_interface.api.*` |
| 因子筛股 | `screener/` | domain（模型/字段/停牌）、application（ScreenerService/BackfillService/BaselineService/ports）、infrastructure（QuestDB 仓储/补齐/队列）、interface；**构建编排已迁到 acquisition** | `screener.interface.api`、`python -m screener` |
| 持仓 | `position/` | `manager`（组合/持仓/交易/盈亏）、`schema`、`modes/`（ATR、GRID）、`llm_tool` | MCP `pm_*` 工具 |
| 因子库 | `factors/` | `library`（`F` 命名空间/`FactorDef`/注册表）、`spec`（`FactorSpec` 元数据）、`ops/ts`（时序算子）、`ops/price_factors`（**85 个价量因子**）、`cross_section`（rank/zscore/winsorize/composite/neutralize）、`panel`（date×symbol 面板）、`naming`（自研命名）、`stored`（screener 列元数据）、`technical`（ATR canonical） | `from factors import F` |
| 因子引擎 | `factor_engine/` | `service`（计算/分批写库/ensure）、`store`（因子表）、`hooks`、`audit`、`wide`（56 列宽表）、`fundamental`（15 成长质量）、**`fin_ext`（61 财务因子）**、`valuation`（13 估值 + PEG）、`definitions/`（内置 + legacy + **`price_factors` 85 价量 spec**）、CLI | `python -m factor_engine` |
| 整合同步 | `acquisition/` | `Scope`、`Orchestrator`（唯一入口）、`syncers`、`datasets/`（daily/financial/benchmark/screener/moneyflow/sentiment/concept）、`build_orchestrator`、进度条、CLI/MCP | `python -m acquisition run/update/status` |
| 策略 | `strategies/` | `base`（单股/横截面基类）、`context`（只读上下文 + 订单意图）、`universe`、`weighting`、`models/`（一文件一策略） | `python -m backtest list/validate/run` |
| 回测 | `backtest/` | `domain`（RunSpec/BacktestResult/报告）、`application`（runner/loader/cross_section/rebalance/run_id/ports/registry）、`infrastructure`（data/engine/execution/output）、`config`/`bootstrap`/`cli`；PyBroker 为 optional extra | `python -m backtest` |
| 研究报告 | `research/` | `report/metrics`（IC/分层/衰减/可成交性）、`diagnostics`（断言/归因）、`definitions`（因子可读定义）、`panel`（PIT 面板）、`rating`（100 分制 + 否决 + 四象限）、`render`（内联 SVG 的 HTML）、`charts`、`runner`、`scorecard` | `python scripts/factor_report.py`、`python scripts/factor_scorecard.py` |
| 分析脚本 | `analysis/` | 事件研究（`archetype` / `earnings_event`）、覆盖度与库体检（`db_coverage` / `db_health`）、因子批量构建 | `python analysis/<script>.py` |
| 技术指标 | `factors/technical/` | 纯 pandas/numpy 的 ATR canonical 实现 | `from factors import ATR` |
| 建表/迁移 | `scripts/` | `init_database.py`、`migrate_database.py`、`migrate_factors.py`、`migrate_market_type.py`、`migrate_market_to_exchange.py`、`factor_audit.py`、`factor_corr.py`、`schemas/`（market/financial/moneyflow/sentiment/concept/factor/position/screener/task） | 手工执行 |
| 测试 | `tests/` | 默认离线、不依赖 DB；联网/DB 用例用 `@require_live` + `RUN_LIVE_TESTS=1` | `pytest` |
| 文档 | `docs/` | `modules/`（11 篇）、`design/`、`guides/`、`data/`、`notes/`、`index.html`（交互图谱） | 见 `docs/README.md` |
| MCP 入口 | `MCPserver.py` | 注册工具、启动 FastMCP（http/sse/stdio） | `python MCPserver.py` |

> 回测的策略/因子写法、设计细节与当前限制见 [`docs/guides/backtest.md`](docs/guides/backtest.md) 与 [`docs/design/backtest_architecture.md`](docs/design/backtest_architecture.md)。

### 1.4 数据流

1. **同步**：`acquisition` 或 `scripts` 把行情/财务写入 QuestDB（不复权价 + `adj_factor`）；
2. **服务**：`data_interface` 读库、按需回源、复权计算；`factor_engine` 写 `factor_value_*` 与 `factor_daily_wide`，`screener` 只读筛选；
3. **在线消费**：MCP 工具（行情、财务、筛股、持仓、任务）统一走 `api/runtime.py` 单例；
4. **离线消费**：回测从 QuestDB 只读加载 → 因子面板 → 策略/引擎 → 自建账本 → console/csv；
5. **可复现**：回测 `run_id` 覆盖策略、参数、因子、数据快照、执行规则与配置。

---

## 2. 5 分钟快速开始

### 2.1 Docker（推荐）

```bash
cp config.example.json config.json   # 填入 TUSHARE_TOKEN 等
docker compose up -d
```

启动顺序：

1. `questdb` 启动；
2. `db-init` 一次性执行 `scripts/init_database.py`，创建全部表（含 screener、情绪表）；
3. `mcp-server` 等 `db-init` 成功后启动，**运行期不再建表**。

查看状态：

```bash
docker compose ps
docker logs mcp-app --tail 100
```

> 首次启动 `mcp-app` 会在容器内安装依赖（`uv sync --frozen`），网络较慢时可能需要几分钟；依赖保存在 `mcp_venv` 命名卷中。

### 2.2 本地运行（非 Docker）

前提：QuestDB 已启动并监听 `8812`。

```bash
# 1. 安装依赖
uv sync

# 2. 准备配置（config.json 已被 .gitignore 排除）
cp config.example.json config.json

# 3. 初始化数据库（只需执行一次）
python scripts/init_database.py

# 4. 启动 MCP 服务
python MCPserver.py
```

`config.example.json` 里 `questdb.host = "localhost"`；在 Docker 内则用 `questdb`（程序会自动把宿主机上的 `questdb` 回退成 `localhost`）。

> MCPserver **不会**自动建表。未执行初始化脚本会报“表不存在”。

### 2.3 单独初始化 Screener

```bash
python scripts/init_database.py --only screener
```

会创建四张 screener 表：

- `factor_daily_wide`（原 `screener_daily`）
- `intraday_baseline`
- `screener_build_state`
- `market_suspension`

因子长表单独初始化：

```bash
python scripts/init_database.py --only factor
```

会创建 `factor_registry`、`factor_alias`、`factor_value_daily`、`factor_value_announce`、`factor_value_latest`、`factor_build_state`、`factor_audit_state`。

### 2.4 单独初始化分档 / 情绪 / 概念

```bash
python scripts/init_database.py --only moneyflow,sentiment,concept
```

会创建 `moneyflow_daily`、`moneyflow_source`、`margin_detail`、`top_list`、`block_trade`、`concept_board_dim`、`concept_board_daily`、`concept_member_snapshot`、`concept_classification`、`concept_board_alias`。

---

## 3. 配置说明

所有可变配置都在 `config.json`（模板见 `config.example.json`）。**`config.json` 已被 gitignore，请勿提交。**

```json
{
  "questdb": {"host": "localhost", "port": 8812, "rest_port": 9000},
  "tushare": {"enabled": true, "token": "<YOUR_TUSHARE_TOKEN>", "points": 2120},
  "market": {
    "incremental_source_priority": ["baostock", "akshare", "tushare"]
  },
  "screener": {
    "benchmark": {"code": "000300", "market": "IDX"},
    "daily": {"beta_window": 252, "atr_window": 14, "formula_version": "daily-v1"},
    "baseline": {"freq": "5m", "lookback_days": 20},
    "backfill": {"max_lookback_bars": 260, "sync_budget_ms": 2000}
  },
  "acquisition": {"include_funds": true, "empty_day_retry_limit": 3},
  "server": {"transport": "http", "host": "0.0.0.0", "port": 8080}
}
```

| 配置段 | 关键项 | 说明 |
|---|---|---|
| `questdb` | `host` / `port` / `rest_port` | PG wire `8812`，HTTP API `9000` |
| `tushare` | `token` / `points` / `requests_per_minute` | 财务与复权因子数据源；至少 2000 积分 |
| `market` | `incremental_source_priority` | 行情增量源顺序：`baostock` / `akshare` / `tushare` |
| `screener.benchmark` | `code` / `market` | Beta 基准，默认沪深 300 `000300` |
| `screener.daily` | `beta_window` / `atr_window` / `formula_version` | 日频因子窗口与公式版本 |
| `screener.baseline` | `freq` / `lookback_days` | 分时基线频率与样本窗口 |
| `screener.backfill` | `max_lookback_bars` / `sync_budget_ms` / `fetch_benchmark` | 回看上限、读路径预算、是否同步拉基准 |
| `financial` | `statement_start_year` / `disclosure_lag_days` | 财报范围与按报告期区分的披露滞后 |
| `acquisition` | `daily_batch_days` / `include_funds` / `empty_day_retry_limit` | 批次大小、是否含 ETF/LOF、空数据日重试 |
| `server` | `transport` / `host` / `port` | 默认 Streamable HTTP `8080`（也支持 `sse` / `stdio`） |
| `logging` | `directory` / `file` / `console_level` | 日志轮转 |

环境变量覆盖：`QUESTDB_HOST`、`QUESTDB_PORT`、`TUSHARE_TOKEN`。
**推荐把 token 放环境变量**，`config.json` 只留占位符。

### 3.1 限流与错误语义

- 主动限流：按 `tushare.requests_per_minute` 控制频率（进程内单例，全服务共用）；
- `report_rc`：本地配额文件 + 独立限流器（每天 10 次），超限自动降级 AkShare；
- 估值：Tushare `daily_basic` 限流/无权限时自动降级 AkShare 估值；
- 错误统一返回结构化 `error_details`：

```json
{
  "success": false,
  "message": "Tushare 接口 income 今日总量已达上限",
  "error_details": {"type": "DailyQuotaExceededError", "interface": "income", "limit": 100000}
}
```

---

## 4. 数据口径与存储契约

这一节是“数据是否可信”的核心约定。

### 4.1 价格：存不复权价 + 复权因子

- `ohlcv_*` 表只存 **不复权（raw）价**，另存 `adj_factor`（累计后复权因子）。
- 复权在**读取时**计算，除权除息后**不需要回刷历史**：
  - `hfq = price × adj_factor`
  - `qfq = price × adj_factor / anchor`，`anchor` 是该标的**最新**因子（保证同一天在不同查询区间返回一致的前复权价）。
- 因子拿不到（如 ETF 无稳定因子源）时写 `NULL`，读取端返回 `adjust_fallback: "missing_adj_factor"` 且**不做价格变换**。
- 行情增量源可配置 `baostock` / `akshare` / `tushare`。Tushare 仅支持股票日/周/月线，返回不复权价；分钟线仍由 AkShare/BaoStock 提供。
- **交易所与品种**：2026-10-31 起 `market` 列存交易所（`SSE` / `SZSE` / `BSE`），`type` 列存品种（`stock` / `etf` / `lof` / `fund` / `idx`）；OHLC 的 DEDUP 键为 `(ts, code, market, type)`。`MLCN` / `IDX` 只作为 API 的历史范围标记保留，由 `market_scope_predicate()` 翻译。

| 场景 | 使用口径 | 原因 |
|---|---|---|
| 行情展示、ATR/止损、技术指标、Screener | `qfq`（默认） | 连续序列，除权日无假跳空 |
| 持仓盈亏、成交价核对 | `none`（不复权） | 成本价是真实成交价，必须同口径 |
| 市值计算 | 不复权收盘价 | 股本 × 真实价格 |

### 4.2 单位

| 字段 | 单位 |
|---|---|
| `volume` | 股（东财/腾讯的「手」已 ×100） |
| `amount` | 元（Tushare 千元已 ×1000） |
| `turnover` | %（换手率百分数；新浪的比值已 ×100） |
| 财务金额 | 元（万元/亿元已换算） |
| `dividend_yield` | 小数（3.59% → 0.0359） |

> 情绪三表保留 Tushare 原始口径：`margin_detail` / `top_list` 金额为**元**；
> `block_trade` 的 `price` 为元、`vol` 为万股、`amount` 为万元。

### 4.3 时间

- 统一 **Asia/Shanghai**，写库为 naive 北京时间（与 `trade_date` 语义一致），不依赖容器 TZ。
- Screener 的 `BuildState` 租约、`retry_at`、`computed_at` 使用 `screener/domain/clock.py` 的北京时间。
- 周/月线用 `Period` 作为周期身份，且只生成**已完成**周期，不会写入半根 bar。

### 4.4 财务指标

- 累计报表按 Q1→H1→Q3→FY **相邻报告期**相减拆单季；跳期无法拆分的置 `None` 并标 `GAP`。
- TTM 必须**连续 4 个季度且值非空**，否则返回 `None`（PE/PS/EV 一起为空），不输出虚高数值。
- 估值按 Point-in-Time：每个报告期取 `publish_date <= as_of` 的**最新版本**，财报重述不会让历史季度消失。
- 指标每次都用**全量历史**重算，并做 None 保护（不会把已有同比覆盖成空）。

---

## 5. Screener 因子筛股

### 5.1 能力

Screener 提供：

- **日频筛选因子**：涨跌、跳空、量比、均量、ATR、波动率、均线、52 周高低、Beta、市值等；计算由 `factor_engine` 负责，screener 只读；
- **因子长表**：`factor_value_daily` / `factor_value_latest`，支持任意 `factor_id` 与版本；
- **分时量能基线**：按分钟/5 分钟桶统计历史平均成交量与累计成交量，用于相对成交量、时间相对交易量；
- **读路径自动补齐**：DB 优先，缺口才回源，取到先落库再计算；
- **可恢复构建状态**：`screener_build_state` 记录进度/游标/质量/重试，进程重启后可恢复；
- **停牌/退市/NoData**：`market_suspension` 避免把停牌当缺口反复回源；
- **MCP + CLI 双入口**：Agent 可调用工具，运维也可手工补数。

### 5.2 分层架构

```text
interface
  api.py / cli.py / worker_main.py
        │
application
  ScreenerService / BackfillService / BaselineService / ports.py
        │
domain
  models / build_state / suspension / fields / factors /
  baseline / coverage / clock
        ▲
        │ 实现端口
infrastructure
  acquisition.py（行情读取端口） / repository / query_builder /
  job_queue / mappers
        │
        └── data_interface（共享行情/QuestDB/交易日历）
```

> 构建编排 `BuildOrchestrator` 已迁到 `acquisition/build_orchestrator.py`；
> 56 列因子计算在 `factor_engine/wide.py`，screener 只读筛选。

依赖规则：

- `domain` 不 import `data_interface`、`requests`、`psycopg2` 等基础设施，也不依赖外部回测框架；
- `application` 只依赖 `ports`，不直接 import infrastructure；
- `infrastructure` 实现端口，可以复用 `data_interface`；
- `bootstrap.py` 是唯一装配具体实现的地方。

> 表结构见 §8.4；因子字段与构建规则以 `factor_engine/wide.py`、
> `scripts/schemas/screener_schema.py` 为准。

---

## 6. 回测（Backtest）

回测是独立的离线链路，与 MCP 运行时解耦；PyBroker 是 **optional extra**：

```bash
uv sync --extra backtest
```

### 6.1 设计目标

- 策略只依赖「策略 API + 因子库」，不 import PyBroker / QuestDB / `data_interface`；
- 数据源、执行引擎、输出都是可替换插槽（ports + registry + 配置名）；
- 单股与横截面共用账本、A 股执行规则与输出；
- 指标以**自建账本**为准，PyBroker 的 `metrics_df` 只作对照；
- 同一输入（策略源码/参数/因子/数据快照/规则/配置）必须得到同一个 `run_id`。

### 6.2 分层架构

```text
strategies/（纯 Python）              factors/（纯 pandas）
  base / context / universe             library / ops / cross_section / panel
  weighting / models/*.py               （复用 factors.ops 与 screener 字段）
        │                                      │
        └──────────────────┬───────────────────┘
                           ▼
              backtest/application
   runner / loader / cross_section / rebalance / run_id / ports / registry
                           │
              backtest/infrastructure
   data/       QuestDBDataSource / MemoryDataSource / snapshot
   engine/     PyBrokerEngine（唯一 import pybroker 的文件）
   execution/  a_share（T+1/整手/涨跌停/停牌/费用）/ ledger（PortfolioLedger）
   output/     console / csv
                           │
                     backtest/domain
          RunSpec / BacktestResult / ExecutionReport / errors
```

| 文件 | 职责 |
|---|---|
| `backtest/application/runner.py` | 解析 universe、加载行情、建因子面板、算 `run_id`、调用引擎、输出 |
| `backtest/application/cross_section.py` | 横截面预选：向量化排名 → 目标权重 → 候选集/每日 ranks/selections |
| `backtest/application/loader.py` | 按路径加载策略 `.py`，校验模块级 `STRATEGY`，合并 `--param` |
| `backtest/application/rebalance.py` | 日/周/月/每 N bar 的调仓日门控 |
| `backtest/infrastructure/engine/pybroker_engine.py` | 单股/横截面执行、A 股校验、下单、T+1 台账同步 |
| `backtest/infrastructure/execution/a_share.py` | 涨跌停价、整手、T+1 可卖数量、费用模型 |
| `backtest/infrastructure/execution/ledger.py` | 多标的账本、`equity/metrics`、公司行为钩子、对账 |
| `backtest/infrastructure/output/*.py` | `console` / `csv` 输出 |
| `backtest/application/run_id.py` | 规范化 payload + SHA-256，保证同输入同 id |

### 6.3 两条执行路径

**单股**：

```text
数据（start-warmup..end）→ 因子面板 → Strategy.add_execution(exec_fn)
  → on_bar 产生订单意图 → A 股校验（整手/涨跌停/停牌/T+1/现金）
  → 下一根 bar 成交（默认 open，可配 close）→ T+1 台账 → PortfolioLedger → metrics
```

**横截面（混合架构）**：

```text
全 universe 数据 → 因子面板
  → plan_cross_section：每个调仓日 rank → top_n → weights
     产出 targets / 每日 ranks / candidates / selections
  → PyBroker：add_execution(risk_fn, candidates)
              set_after_exec(rebalance_fn)
     risk_fn     ：只对持仓调 on_bar（风控退出）
     rebalance_fn：调仓日按 targets 先卖后买；非调仓日只做 buffer 退出
  → PortfolioLedger → metrics
```

- **候选集 = 所有调仓日入选标的的并集**，只有候选集进入 PyBroker；
- 横截面不使用 PyBroker 的 `stop_loss/stop_trailing`（入场在 `after_exec`，无法绑定 entry），
  风控退出由策略在 `on_bar` 里 `close_position()`；
- buffer 卖出阈值为 `rank > top_n + buffer`，只在非调仓日执行，不重置目标权重；
- 调仓日先卖后买，卖出回款计入买入预算（PyBroker 默认卖单先于买单结算）。

### 6.4 策略与因子写法

策略是一个 `.py` 文件，模块级 `STRATEGY` 必须是**类或工厂**（不能是实例），每次 run 实例化：

```python
# strategies/models/atr_trend.py（单股）
from strategies import SingleSymbolStrategy
from factors import F

class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ("600519",)
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"]), F.ma(20)]

    def on_bar(self, ctx):
        ...

STRATEGY = AtrTrend
```

```python
# strategies/models/rank_mom20.py（横截面）
from strategies import CrossSectionStrategy, Universe, Rebalance, EqualWeight
from factors import F

class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    universe = Universe.explicit(("600519", "000001", "300750"))
    top_n = 2
    buffer = 1
    rebalance = Rebalance.WEEKLY
    weighting = EqualWeight()
    params = {"n": 20}

    def factors(self):
        return [F.mom(self.params["n"], on="hfq_close")]

    def rank(self, panel, dt):
        return panel.at(dt, f"mom{self.params['n']}")

STRATEGY = RankMom20
```

因子只声明、不计算：`F.mom / F.ma / F.atr / F.vol` 逐 bar 现算；横截面变换用 `F.rank / F.zscore`。
读取预计算因子用 `F.factor(id)`，可用 `materialization` 指定消费方式：

```python
def factors(self):
    # auto：stored 优先，缺了且有 compute 就逐 bar 现算
    return [
        F.factor("momentum.ret"),
        F.factor("technical.pct_chg", materialization="stored"),  # 必须读预计算
        F.factor("momentum.ret", materialization="inline"),        # 强制逐 bar 现算
    ]
```

- `stored`：必须已预计算，缺数据启动报错
- `inline`：不读库，逐 bar 现算（需有 compute）
- `auto`：默认，stored 优先，缺失回退 inline

`F.stored(...)` 仅保留为 legacy screener 宽表列兼容入口，新策略请用 `F.factor`。

### 6.5 数据与执行口径

| 项 | 口径 |
|---|---|
| `close` | 不复权，用于成交、整手、涨跌停、盈亏 |
| `hfq_close` | `close × adj_factor`，用于因子计算 |
| `adj_close` | 前复权，展示/兼容；依赖取数区间 anchor |
| `adj_factor` | 累计后复权因子 |
| `volume` / `amount` | 股 / 元 |

- 因子在 `hfq_close` 上计算；信号在 T 日收盘产生，默认 T+1 开盘成交（`fill=next_open`），
  可配 `fill=close`（下一根 bar 收盘）；PyBroker 默认 `PriceType.MIDDLE` 不会出现在主路径；
- **T+1**：卖单下一根 bar 成交，台账区分 `available`（已解禁）与 `sellable`（含当日成交）；
- **整手**：买入向下取整 100 股，卖出允许零股；
- **涨跌停**：主板 10%、创业板/科创板 20%、北交所 30%，按 `prev_close` 计算；
  ST 5% 与新股无涨跌幅**尚未接线**（当前按非 ST、非新股处理）；
- **停牌**：`volume<=0` 或 `close` 无效不下单，持仓按最近收盘估值；
- **费用**：佣金 0.025%（最低 5 元，双边）、过户费 0.001%（双边）、
  印花税卖出 0.1%（2023-08-28 前）/ 0.05%（之后）；
- **账本**：`PortfolioLedger` 回放成交，按不复权 `close` 估值；公司行为当前只支持 `off`。

### 6.6 CLI 用法

```bash
# 列出 / 校验策略
python -m backtest list
python -m backtest validate strategies/models/atr_trend.py

# 单股回测
python -m backtest run strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --out console,csv

# 横截面回测（当前仅 explicit universe）
python -m backtest run strategies/models/rank_mom20.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --out console,csv

# 覆盖参数
python -m backtest run strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 --param n=10 --param k=1.5
```

CSV 默认写入 `output/backtest/<run_id>/`：`metrics.json`、`equity.csv`、`orders.csv`、
`trades.csv`、`selections.csv`、`rejected.csv`、`unfilled.csv`、`corporate_actions.csv`。

### 6.7 输出与 `run_id`

- `BacktestResult`：`run_id / spec / metrics / metrics_pybroker / equity / orders /
  trades / selections / factor_meta / execution_report / meta`；
- `metrics` 由自建账本产出；`metrics_pybroker` 仅对照；
- `run_id` 覆盖：策略源码 hash、策略参数、因子定义、数据快照（全 OHLCV + amount + adj_factor）、
  universe、日期区间、执行规则、`fill`、`corporate_actions`、`mode/top_n/buffer/weighting`、
  引擎与库版本、`seed`；
- 同一 `run_id` 重复运行结果一致（已实测宿主机/容器、重复运行均相同）。

### 6.8 配置

`config.json` 可选 `backtest` 段（不写则用默认值）：

```json
"backtest": {
  "engine": "pybroker",
  "data": "questdb",
  "factor_source": "auto",
  "outputs": ["console", "csv"],
  "output_dir": "output/backtest",
  "initial_cash": 1000000,
  "warmup_min_bars": 260,
  "fill": "next_open",
  "benchmark": "000300",
  "rebalance": "weekly",
  "max_gross_exposure": 0.98,
  "allow_short": false,
  "seed": 0,
  "corporate_actions": "off",
  "rules": {
    "lot_size": 100,
    "t_plus_1": true,
    "limit_up_down": true,
    "suspension": true,
    "commission_rate": 0.00025,
    "min_commission": 5.0,
    "transfer_fee_rate": 0.00001,
    "stamp_tax_schedule": [
      {"effective_from": "1990-01-01", "rate": 0.001},
      {"effective_from": "2023-08-28", "rate": 0.0005}
    ]
  }
}
```

配置在加载/运行前校验：未知字段、非法 `fill/factor_source/corporate_actions`、
`max_gross_exposure ∉ (0,1]` 都会抛 `ConfigError`。以下开关**会明确失败**而不是静默忽略：
`corporate_actions="approx"`、`allow_short=true`、`factor_source="stored"`。
`benchmark` 目前只记录到 `spec/meta`，尚未产出 alpha/beta（`meta["benchmark_used"]=false`）。

### 6.9 当前限制与注意事项

| 限制 | 现状 |
|---|---|
| universe | 只支持 `explicit`；`all_a/index/file` 会明确报错 |
| 横截面风控 | `on_bar` 只对**持仓**调用；开仓统一在 `after_exec`；不使用 PyBroker 止损 |
| 再平衡 | 支持 `daily/weekly/monthly/every_n_bars`；`DRIFT` 未实现 |
| 候选集规模 | 候选集 = 所有调仓日入选标的并集，随机排名下接近全 universe（500×5y 实测 497/500，约 41s；2000×5y 1492/2000，约 121s）。全 A 需先做流动性预筛或分块执行 |
| ST / 新股 | 涨跌幅规则未接历史 ST/上市日，当前按非 ST、非新股处理 |
| 公司行为 | 只支持 `off`；没有分红/送转调整，长期回测收益会偏保守 |
| benchmark | 只记录代码，不产出 alpha/beta/tracking error |
| 优化 / 研究 | `optimize/walkforward/research` 尚未接入 CLI |
| 输出 | 只有 `console/csv`；`html/questdb` 未实现 |
| 滑点 | 无滑点模型；成交价取 open/close 并四舍五入到分 |
| 测试 | 离线单测（`pytest`，默认不联网/不依赖 DB）；联网/DB 用例需 `RUN_LIVE_TESTS=1` |

更细的策略 API、输出字段与设计取舍见 [`docs/guides/backtest.md`](docs/guides/backtest.md)
和 [`docs/design/backtest_architecture.md`](docs/design/backtest_architecture.md)。

---

## 7. 代码结构

> **模块级文档见 [docs/README.md](docs/README.md)**：每个模块一篇，固定包含「架构与依赖」和「核心类/方法、用法、注意事项」两节，面向 AI 阅读。


```text
FinBridge/
├── MCPserver.py                    # MCP 入口：注册工具 + 启动
├── config.example.json             # 配置模板（真实 config.json 不入库）
├── data_interface/
│   ├── common/
│   │   ├── config.py               # 配置加载（按路径缓存 + 深拷贝）
│   │   ├── questdb_client.py       # QuestDBClient（参数化 SQL、异常可见）
│   │   ├── security_code.py        # 代码/交易所/板块判定（唯一实现）
│   │   ├── timeutil.py             # 统一北京时间
│   │   ├── trading_calendar.py     # 交易日历（失败退避 + 工作日兜底）
│   │   ├── baostock_client.py      # BaoStock 共享会话
│   │   ├── rate_limiter.py         # 滑动窗口限流
│   │   └── exceptions.py           # 统一异常
│   ├── market/
│   │   ├── adjust.py               # 复权换算（qfq/hfq/none）
│   │   ├── adj_factors.py          # 复权因子提供者（Tushare→AkShare）
│   │   ├── constants.py            # 表名/频率/列顺序
│   │   ├── fetcher.py              # MarketDataFetcher
│   │   ├── loader.py               # MarketDataLoader（增量 + 复权）
│   │   └── sources/                # akshare / baostock / tushare / tencent / index
│   ├── financial/
│   │   ├── fields.py               # canonical 字段字典
│   │   ├── normalizer.py           # 字段/单位/派生/校验
│   │   ├── metrics.py              # TTM / EV / 估值倍数
│   │   ├── repository.py           # FinancialRepository
│   │   ├── service.py              # FinancialDataService
│   │   └── sources/                # tushare / akshare
│   ├── moneyflow/                  # 个股日频分档（资金流）
│   │   ├── service.py              # 同步/质量/查询
│   │   ├── repository.py           # MoneyflowRepository
│   │   └── sources/                # tushare.moneyflow
│   ├── sentiment/                  # 两融 / 龙虎榜 / 大宗
│   │   ├── service.py              # 按交易日批量同步 + 映射
│   │   └── repository.py           # margin_detail / top_list / block_trade
│   ├── concept/                    # 概念板块
│   │   ├── service.py              # 列表/日线/成分/分类
│   │   ├── repository.py           # ConceptRepository（PIT 快照）
│   │   └── sources/                # ths / em / ths_scrape / tushare.concept
│   └── api/
│       ├── runtime.py              # 进程内单例装配
│       ├── market_tools.py         # 行情 MCP 工具
│       ├── financial_tools.py      # 财务 MCP 工具
│       ├── moneyflow_tools.py      # 分档 MCP 工具
│       ├── concept_tools.py        # 概念板块 MCP 工具
│       └── utils.py                # 统一响应信封
├── screener/
│   ├── bootstrap.py                # composition root
│   ├── config.py
│   ├── domain/                     # 纯模型/公式/状态机
│   ├── application/                # 用例编排 + ports
│   ├── infrastructure/             # QuestDB 仓储/补齐/SQL/队列
│   └── interface/                  # MCP tools / CLI / worker_main
├── acquisition/                   # 整合同步（唯一入口：Scope/编排/CLI/MCP）
│   ├── orchestrator.py             # DATASET_ORDER / Orchestrator / Scope
│   ├── syncers.py                  # 数据集 → data_interface / factor_engine 适配
│   ├── datasets/                   # daily/financial/benchmark/screener/moneyflow/sentiment/concept
│   ├── build_orchestrator.py       # screener 构建编排
│   ├── api.py                      # MCP acquisition_run / acquisition_status
│   └── state.py
├── position/                       # 组合/持仓/交易
│   ├── manager.py / schema.py / llm_tool.py
│   └── modes/                      # ATR / GRID
├── factors/                        # 因子库（纯计算，不依赖 DB）
│   ├── library.py                  # F 命名空间 / FactorDef / @factor
│   ├── spec.py / registry.py       # FactorSpec 元数据 / 全局注册表与拓扑排序
│   ├── ops/
│   │   ├── ts.py                   # 时序算子
│   │   └── price_factors.py        # 85 个价量因子（liquidity/momentum/reversal/risk/size）
│   ├── cross_section.py            # rank / zscore / winsorize / composite / neutralize / nl_size
│   ├── panel.py                    # 长表 -> date×symbol 面板（stored + 派生算子）
│   ├── naming.py                   # 自研命名：category.semantic[_window]
│   ├── taxonomy.py                 # Category 枚举 + 子维度白名单
│   ├── technical/                  # ATR canonical
│   └── stored.py                   # screener 列元数据
├── factor_engine/                  # 因子计算/存储/缓存/对账
│   ├── service.py / store.py / data.py / hooks.py / audit.py
│   ├── wide.py / wide_store.py     # 56 列宽表计算与写路径
│   ├── fundamental.py              # 15 个成长/质量因子（PIT + TTM）
│   ├── fin_ext.py                  # 61 个财务因子（Quality 53 + Value 8）
│   ├── valuation.py                # 13 个估值因子
│   └── definitions/                # price.py（内置）+ legacy.py（23）+ price_factors.py（85 spec）
├── strategies/                     # 策略 API
│   ├── base.py / context.py / universe.py / weighting.py
│   └── models/                     # 一文件一策略
├── backtest/                       # 离线回测 / 研究
│   ├── domain/                     # RunSpec / BacktestResult / 报告
│   ├── application/                # runner / loader / cross_section /
│   │                               # rebalance / run_id / ports / registry
│   ├── infrastructure/
│   │   ├── data/                   # questdb / memory / snapshot
│   │   ├── engine/                 # pybroker_engine（唯一 import pybroker）
│   │   ├── execution/              # a_share / ledger
│   │   └── output/                 # console / csv
│   ├── config.py / bootstrap.py / cli.py / __main__.py
├── research/                       # 因子研究报告与评分卡
│   └── report/
│       ├── metrics.py              # IC/ICIR/NW-t/分层/衰减/可成交性检验
│       ├── diagnostics.py          # 断言、归因、可复现性检查
│       ├── definitions.py          # 因子可读定义与对照组
│       ├── panel.py                # 点位池面板与 PIT 数据
│       ├── rating.py               # 100 分制 + 一票否决 + 时间×空间象限
│       ├── render.py / charts.py   # 内联 SVG 的 HTML 报告
│       ├── runner.py / db.py       # 单因子端到端 / QuestDB 访问
│       └── scorecard.py            # factor_scorecard 建表与写入
├── analysis/                       # 事件研究 / 覆盖度 / 数据库体检
│   ├── archetype.py / earnings_event.py
│   ├── db_coverage.py / db_health.py
│   └── README.md
├── scripts/
│   ├── init_database.py            # 建表
│   ├── migrate_database.py         # 存量库迁移
│   ├── migrate_factors.py          # 旧 screener 表迁移
│   ├── migrate_market_type.py      # market/type 列迁移
│   ├── migrate_market_to_exchange.py # market=交易所 迁移
│   ├── backfill_factors.py         # 财务因子物化（STORED）
│   ├── factor_report.py            # 单因子报告
│   ├── factor_scorecard.py         # 全量评级 + 评分卡入库
│   ├── factor_audit.py             # 全库断言/归因报告
│   ├── factor_corr.py              # 相关矩阵/家族聚类
│   ├── calibrate_directions.py     # 用实测 IC 校准方向
│   └── schemas/                    # market/financial/moneyflow/sentiment/...
├── docs/                           # 所有设计/模块/变更文档
│   ├── README.md                   # 索引：目录结构 + 架构图 + 依赖规则
│   ├── modules/                    # 11 篇模块文档（架构与依赖 + 核心类/方法）
│   ├── design/                     # 架构/因子库/物化/报告 v2/回测架构/数据管线
│   ├── guides/                     # 回测、选股、output 目录约定
│   ├── data/                       # 数据覆盖度报告
│   ├── notes/                      # 变更备注、迁移与回滚 SQL
│   └── index.html                  # 交互式系统图谱（GitHub Pages 首页）
└── tests/                          # 离线单测（默认不联网、不依赖 DB）
```

---

## 8. 数据库设计与迁移

### 8.1 QuestDB 设计要点

- 每张时序表都有 designated timestamp；
- 按时间分区：`HOUR / DAY / MONTH / YEAR`；
- 行情/财务/Screener 表使用 `WAL + DEDUP UPSERT KEYS`；
- **DEDUP key 必须包含 designated timestamp**；
- 持仓表保留原地 `UPDATE` 语义，因此不使用 WAL/DEDUP。

> **2026-10-31 行情 schema**：`market` 统一存交易所（`SSE` / `SZSE` / `BSE`），
> 品种由 `type` 区分（`stock` / `etf` / `lof` / `fund` / `idx`）；OHLC 的 DEDUP
> 键包含 `type`，不再写旧值 `MLCN`。存量库用 `migrate_market_type.py` +
> `migrate_market_to_exchange.py` 升级。

### 8.2 行情表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `ohlcv_1m` ~ `ohlcv_60m` | `ts` | HOUR/DAY | `(ts, code, market, type)` | 分钟 OHLCV + `adj_factor`；`market`=交易所，`type`=品种 |
| `ohlcv_1d` / `1w` / `1mo` | `ts` | MONTH/YEAR | `(ts, code, market, type)` | 日/周/月 OHLCV、换手率、`adj_factor`；含 ETF/LOF/指数 |
| `stock_basic_info` | `ts` | MONTH | `(code, ts)` | 基础信息；`exchange` 与 `market` 均为交易所（兼容字段） |
| `fund_basic_info` | `ts` | MONTH | `(code, ts)` | ETF/LOF 资料 |
| `moneyflow_daily` | `ts` | MONTH | `(ts, code, market, source)` | 个股日频分档；`market`=交易所 |
| `moneyflow_source` | `updated_at` | YEAR | `(updated_at, source)` | 分档源口径/覆盖/优先级 |
| `margin_detail` | `ts` | YEAR | `(ts, code)` | 两融余额、融资买入/偿还、融券余额/余量 |
| `top_list` | `ts` | YEAR | `(ts, code)` | 龙虎榜；同股同日多条按旧键合并 |
| `block_trade` | `ts` | YEAR | `(ts, code, buyer)` | 大宗交易；同买方多笔会合并 |
| `concept_board_dim` | `ts` | YEAR | `(ts, board_code, source)` | 概念板块维度（日快照） |
| `concept_board_daily` | `ts` | MONTH | `(ts, board_code, source)` | 板块日线（OHLCV+成交额） |
| `concept_member_snapshot` | `ts` | YEAR | `(ts, board_code, stock_code, source)` | 成分快照（PIT） |
| `concept_classification` | `ts` | YEAR | `(ts, concept_code, source)` | 概念分类树 |
| `concept_board_alias` | `updated_at` | YEAR | `(updated_at, board_code, source, source_code)` | 跨源板块代码映射 |
| `concept_boards` / `_ohlc` / `_constituents` | — | — | — | **已拆除（2026-10-30 DROP）**，由 `concept_board_*` 取代 |
| `trading_days` | `ts` | YEAR | `(ts)` | 交易日历 |

### 8.3 财务表

| 表 | 时间轴 | DEDUP KEY | 说明 |
|---|---|---|---|
| `fin_report_meta` | `period_end` | `(period_end, report_id)` | 报表版本/披露日/质量状态 |
| `fin_income_statement` | `period_end` | `(period_end, report_id)` | 利润表 |
| `fin_balance_sheet` | `period_end` | `(period_end, report_id)` | 资产负债表 |
| `fin_cash_flow` | `period_end` | `(period_end, report_id)` | 现金流量表 |
| `fin_indicator` | `period_end` | `(period_end, code, report_type)` | 派生指标 |
| `fin_estimate` | `as_of` | `(code, metric, fiscal_year, fiscal_period, as_of, source)` | 一致预期 |
| `fin_valuation_daily` | `ts` | `(ts, code, source)` | 每日估值 |
| `fin_sync_state` | `updated_at` | `(updated_at, code, dataset)` | 同步状态 |
| `fin_raw_report` | `fetched_at` | `(fetched_at, report_id, source)` | 原始报文 |
| `dim_fin_field` / `dim_board` / `map_stock_board` | 见 DDL | — | 字段字典与板块 |
| `fin_disclosure_calendar` | `ts` | `(ts, code, report_date, source)` | 财报披露日历（PIT） |
| `announcement` | `ts` | `(ts, code, art_code)` | 公告列表 |

### 8.4 Screener / 因子表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `factor_daily_wide` | `ts` | MONTH | `(ts, code, market)` | 日频筛选宽表（原 `screener_daily`）；`market`=交易所 |
| `factor_value_daily` | `ts` | MONTH | `(ts, code, factor_id, version, source_version)` | 因子长表（日频） |
| `factor_value_announce` | `ts` | YEAR | `(ts, code, factor_id, report_period, version, source_version)` | 公告类因子（PIT） |
| `factor_value_latest` | `ts` | YEAR | `(ts, code, factor_id, version, source_version)` | 每 code×factor 最新值 |
| `factor_registry` | `updated_at` | YEAR | `(updated_at, factor_id)` | 因子规格目录 |
| `factor_alias` | `updated_at` | YEAR | `(updated_at, alias)` | legacy/简写别名 |
| `factor_build_state` | `updated_at` | YEAR | `(updated_at, job_id)` | 因子构建/重试状态 |
| `factor_audit_state` | `updated_at` | YEAR | `(updated_at, audit_id)` | 投影/增量对账 |
| `factor_scorecard` | `ts` | MONTH | `(ts, run_id, factor_id)` | 因子评级/评分/IC/方向校准 |
| `intraday_baseline` | `as_of_date` | MONTH | `(as_of_date, code, market, freq, minute_end)` | 分时量能基线 |
| `screener_build_state` | `updated_at` | YEAR | `(updated_at, build_id)` | 构建状态（append-only） |
| `market_suspension` | `updated_at` | YEAR | `(updated_at, code, market, status, start_date)` | 停牌/退市/NoData |

### 8.5 持仓表与任务状态

| 表 | 时间轴 | 说明 |
|---|---|---|
| `portfolios` | `id` | 组合现金、总资产、浮盈/已实现盈亏 |
| `positions` | `id` | 当前持仓、成本、市值、止损、模式参数 |
| `trade_records` | `trade_date` | 买卖/调整流水（软删除） |
| `scheduled_task_state` | `updated_at` | 各数据集同步游标、状态、进度、pending |

### 8.6 建表与迁移

```bash
python scripts/init_database.py                  # 全部建表
python scripts/init_database.py --only market    # 只建行情表
python scripts/init_database.py --only sentiment # 只建两融/龙虎榜/大宗
python scripts/init_database.py --only screener  # 只建 Screener 表
python scripts/init_database.py --only factor    # 只建因子表
python scripts/init_database.py --dry-run        # 只打印 DDL

python scripts/migrate_database.py --check       # 存量库体检
python scripts/migrate_database.py --apply       # 加列 / 修类型
python scripts/migrate_database.py --apply --truncate-ohlc   # 清空旧前复权数据

# 2026-10-31：行情表加 type 列、market 改为交易所
python scripts/migrate_market_type.py
python scripts/migrate_market_to_exchange.py
```

> QuestDB 的 `CREATE TABLE IF NOT EXISTS` **不会**给已有表加列，也不会改列类型，
> 所以存量库必须走 `migrate_database.py`。Screener 是新增表，`--only screener` 即可。

---

## 9. MCP 工具清单

### 9.1 响应信封（所有工具统一）

```json
{"success": true,  "message": "...", "result": {...}}
{"success": false, "message": "...", "error_details": {"type": "...", "suggestion": "..."}}
```

### 9.2 行情

| 工具 | 用途 |
|---|---|
| `get_ohlc_data` | OHLCV（1m/5m/15m/30m/60m/d/w/m），`adjust=none\|qfq\|hfq` |
| `get_ticker_basic_info` | 股票/ETF/LOF 基础信息 |
| `get_realtime_quote` | 腾讯单只实时行情（不复权） |
| `get_intraday_metrics` | 盘中指标：涨跌停价、封板时间、开板次数、集合竞价量、VWAP 偏离、封板质量 |
| `get_trading_days` | 交易日列表 |
| `get_today_date` | 今天日期（北京时间） |

### 9.3 财务

| 工具 | 用途 |
|---|---|
| `get_company_profile` | 公司资料、交易所/上市板、行业、板块 |
| `get_financial_statements` | IS / BS / CF / ALL，支持 `as_of` 与 `report_type` |
| `get_financial_indicators` | ROE、毛利率、净利率、增长率、偿债指标 |
| `get_valuation` | PE/PB/PS/PCF/PFCF/EV 系列（缺季时为 `null`） |
| `get_estimates` | 收入/净利润/EPS 一致预期 |
| `get_board_membership` | 所属行业/概念/指数 |
| `sync_board_data` | 同步板块目录或成分股 |
| `sync_financial_data` | 主动同步财务数据 |
| `get_disclosure_calendar` | 财报披露日历（按报告期/代码） |
| `get_upcoming_disclosures` | 未来 N 天将披露的公司 |
| `get_announcements` | 公告列表（按代码/日期区间） |

### 9.4 Screener

| 工具 | 用途 |
|---|---|
| `screen_stocks` | 因子筛选/排序（支持 `spec` 或扁平参数） |
| `get_screener_metrics` | 单只/多只股票的日频因子明细 |
| `get_intraday_baseline` | 分时量能基线 |
| `rebuild_screener` | 提交重建任务（`factor_daily_wide` / `intraday_baseline`） |
| `get_screener_status` | 构建状态/进度 |

### 9.5 持仓

| 工具 | 说明 |
|---|---|
| `pm_create_portfolio` / `pm_list_portfolios` / `pm_modify_portfolio` / `pm_delete_portfolio` | 组合生命周期 |
| `pm_open_position` | 建仓跟踪（`name` 可省略，默认用代码） |
| `pm_execute_trade` | `action` 仅接受 `buy/买入`、`sell/卖出/close`；卖出超过持仓会报错 |
| `pm_close_position` | 按账本实际数量平仓 |
| `pm_modify_position` | `quantity` 以「调整流水」落库 |
| `pm_check_signals` / `pm_get_buying_advice` | 加减仓信号 / 建仓建议 |
| `pm_get_portfolio_status` / `pm_list_modes` | 组合状态、可用模式 |

### 9.6 分档与概念

| 工具 | 用途 |
|---|---|
| `get_moneyflow` | 个股日频分档（四档 × 主动买/卖），返回 `data_source` |
| `get_moneyflow_rank` | 按 `main_amount` / `super_large_net` 等做横截面排名 |
| `get_moneyflow_sources` | 分档源口径、覆盖区间、优先级 |
| `get_concept_boards` | 概念板块列表（支持 `as_of`） |
| `get_concept_members` | 板块成分股（PIT 快照，支持 `as_of`） |
| `get_concept_ohlc` | 板块日线（OHLC + 成交量 + 成交额） |
| `get_concept_classification` | Tushare 概念分类树（可按键词过滤） |
| `get_stock_boards` | 个股所属板块（行业/概念/地域，按 `spt` 分类） |

### 9.7 因子库

| 工具 | 用途 |
|---|---|
| `get_factor_registry` | 因子目录（分类/来源/频率/窗口） |
| `get_factor_values` | 因子长表值（`factor_value_daily`） |
| `get_factor_latest` | 每个代码×因子的最新值 |

> 因子**评级/评分**不通过 MCP 暴露，由 `scripts/factor_scorecard.py` 写入 `factor_scorecard` 表，用 SQL 查询（见 [§13](#13-因子研究报告与评分卡)）。

### 9.8 整合同步

| 工具 | 用途 |
|---|---|
| `acquisition_run` | 一键补齐（可按 datasets/日期/代码/板块限定） |
| `acquisition_status` | 各数据集游标与状态 |

---

## 10. 整合同步（acquisition）

统一入口是 `python -m acquisition` / MCP `acquisition_run`、`acquisition_status`。`datasets` 可选 `trading_calendar`、`stock_basic`、`ohlcv_1d`、`benchmark`、`financial`、`valuation`、`moneyflow`、`margin_detail`、`top_list`、`block_trade`、`concept_meta`、`concept_daily`、`concept_member`、`concept_class`、`factors`、`screener`。

```bash
# 一键全部：raw + screener 宽表 + 因子长表
python -m acquisition run

# 只更新部分数据集
python -m acquisition run --datasets ohlcv_1d,factors,screener

# 两融 / 龙虎榜 / 大宗
python -m acquisition run --datasets margin_detail,top_list,block_trade

# 单代码 + 日期段
python -m acquisition update --code 600519 --start 2024-01-01 --end 2026-10-27

# 单板块 + 日期段
python -m acquisition update --board 300013 --start 2026-10-01 --end 2026-10-27

# 只更新某类
python -m acquisition update --code 600519 --start 2026-10-01 --end 2026-10-27 --datasets factors

# 查看游标/状态
python -m acquisition status
```

> `--loop`：分批数据集（日线/财务/估值/分档/两融/龙虎榜/大宗/概念日线/成分）自动续跑，直到 `done`、`--max-rounds` 或 `--timeout`。
> `--no-financial`：显式排除季度财务；`--force-financial`：即使会重置也运行。
> `run` 默认先检查 `financial` 游标：若本次 `--start` 会改变 `start_year`（触发重置），会自动跳过并告警；日常补数据建议直接加 `--no-financial`。

- **日线**：按交易日调 Tushare `daily` + `daily_basic`（换手率）+ `adj_factor`，可选 `fund_daily` 拉 ETF/LOF；
- **财务**：按股票池分批，逐只增量拉三大报表、重算指标；
- **估值**：按交易日调 Tushare `daily_basic`（一次全市场）+ 批量报表 PIT，写 `fin_valuation_daily`，产出 `valuation.*` 因子；
- **分档**：按交易日调 Tushare `moneyflow`（一次全市场），金额万元→元、量手→股；
- **两融 / 龙虎榜 / 大宗**：按交易日调 Tushare `margin_detail` / `top_list` / `block_trade`（各一次全市场），写 `margin_detail` / `top_list` / `block_trade`；首次接入从库内最新日期续跑，历史回补仍用 `scripts/backfill_history.py --phase p2-structure`；
- **因子长表**：`factors` 数据集调用 `FactorEngineService`，写 `factor_value_daily` / `factor_value_latest`；
- **Screener 宽表**：`screener` 数据集调用 `BuildOrchestrator`，写 `factor_daily_wide`（在 factors 之后）；
- **板块列表/日线**：同花顺（AkShare）一次拿 375 个板块，日线按板块增量；
- **成分**：东方财富优先，失败回退同花顺网页抓取；每次全量写同一快照日；
- **概念分类**：Tushare `concept`，注意该接口限 1 次/小时；
- **空数据日**：未收盘导致的“无数据”记入 `pending` 下次补；情绪类只对最近 3 天重试，避免历史空日永久挂起；
- 游标保存在 `scheduled_task_state`（表名保留兼容），Agent 重启后可继续。

### 10.1 历史回补脚本

```bash
# 分档：自动跳过库内已有交易日，可反复执行
python scripts/backfill_moneyflow.py --start 2022-10-10 --end 2026-10-27

# 概念维度：当前 375 个板块投影回区间端点（source=akshare.ths_index.reconstructed）
python scripts/backfill_concept.py --dataset dim

# 板块日线：逐板块拉取，单板块失败不中断
python scripts/backfill_concept.py --dataset daily --start 2022-10-10 --end 2026-10-27

# 只修复指定板块（例如年度缺口）
python scripts/backfill_concept.py --dataset daily --only-codes 300890,301016 --sleep 0.5

# 统一入口（推荐）：全市场补齐 / 单代码日期段 / 只补某个数据集
python -m acquisition run
python -m acquisition run --datasets ohlcv_1d,moneyflow,factors,screener
python -m acquisition update --code 600519 --start 2024-01-01 --end 2026-10-27
python -m acquisition update --code 600519 --start 2026-10-01 --end 2026-10-27 --datasets screener
# 只预计算指定因子（其余不落库）
python -m acquisition update --code 600519 --start 2026-10-01 --end 2026-10-27 \
  --datasets factors --factor momentum.ret,technical.atr,technical.pct_chg

# 财务因子物化（61 个 STORED 因子；分批控制内存）
python scripts/backfill_factors.py --codes 120 --start 2024-01-01 --chunk 4

# 分时（1m/5m）与指数修复
python scripts/backfill_intraday.py --codes 000949 --freq 5 --start 2026-10-01
python scripts/repair_index_data.py
```

> 概念板块没有历史列表/成分源，`dim` 只能做“当前列表投影”，存在幸存者偏差；
> 2022-10-10 的 dim 含 375 个板块，但当天实际有日线的是 284 个（其余为后续成立），
> 回测应以 `concept_board_daily` 的 `min(ts)` 过滤。日线通过同花顺年度 K 线文件回补，单板块约 1 秒。

Screener 不依赖同步任务：批量 CLI、读路径和 inline worker 都能补齐。

### 10.2 因子引擎

当前 **202 个因子**：5 个内置价格因子 + 23 个 screener legacy 列 + 15 个成长质量因子（PEG 归入 valuation）+ 13 个 valuation 估值因子 + **85 个价量因子（INLINE）** + **61 个财务因子（STORED）**，全部有 compute。基本面因子走 `fin_income_statement/balance_sheet/cash_flow` 的 TTM + PIT 对齐 + 截面排名；valuation 因子直接读 `fin_valuation_daily`。每个因子带 `materialization` 标记（`stored` 必预计算 / `inline` 逐 bar 现算 / `auto` 默认），决定回测时读库还是现算。

```bash
# 列出因子
python -m factor_engine list

# 单代码 / 日期段
python -m factor_engine build --start 2026-10-01 --end 2026-10-27 --codes 600519

# 指定因子
python -m factor_engine build --start 2026-10-01 --end 2026-10-27 \
  --codes 600519 --factor momentum.ret,technical.atr,technical.pct_chg

# 确保某日已就绪（缺则算）
python -m factor_engine ensure --as-of 2026-10-27 --codes 600519
```

---

## 11. 常见问题

### Q1: `Conflict. The container name "/questdb-server" is already in use`

```bash
docker rm -f questdb-server mcp-app mcp-db-init
docker compose up -d
```

### Q2: `QuestDB 未连接` / `Connection refused`

```bash
docker ps
Test-NetConnection localhost -Port 8812
```

本地运行时确认 `config.json` 的 host/port；脚本会自动把 `questdb` 回退到 `localhost`。

### Q3: `表不存在` / `Invalid column: adj_factor`

```bash
python scripts/init_database.py      # 新库
python scripts/migrate_database.py --apply   # 存量库
```

### Q4: 行情价格和券商软件不一致

先确认口径：默认 `adjust="qfq"`（前复权）。持仓盈亏请用 `adjust="none"`。
若返回 `adjust_fallback="missing_adj_factor"`，说明该标的没有因子（常见于 ETF），
此时价格是不复权价，不要再当作前复权使用。

### Q5: `get_valuation` 里 PE/EV 是 null

TTM 要求连续 4 个季度且值非空。缺季、刚上市、报表尚未披露都会返回 `null`——这是
**有意为之**，避免用不完整的季度加总出错误的估值。

### Q6: Tushare 报 `RateLimitExceededError` / `DailyQuotaExceededError`

降低同步频率或等待 `retry_after_seconds`；`get_estimates` 的 `report_rc` 每天仅 10 次，
系统会自动降级 AkShare。

### Q7: `screen_stocks` 返回 0 行

按顺序检查：

1. `allow_partial=false` 默认会过滤 `is_partial=true` 的股票；历史不足时传 `allow_partial=true`；
2. 该交易日快照是否已构建：调用 `get_screener_status` 或 `rebuild_screener`；
3. 是否设置了过严的筛选条件（如 `beta_252`，但基准尚未补齐）；
4. 基准缺失时先运行：

```bash
python -m screener benchmark --start 2025-01-01 --end 2026-10-23
```

### Q8: `ScreenerNotBuilt`

`auto_build=false` 且该交易日快照不存在。先调用 `rebuild_screener`，或重新请求时设
`auto_build=true`。

### Q9: Screener 首次全市场构建很慢

首次需要回填历史行情，受第三方源限速影响。建议：

```bash
python -m screener daily --start 2025-01-01 --end 2026-10-23
```

分批跑，或使用 `repair`/后台 worker；不要在 MCP 请求里同步等待全市场。

---

## 12. 开发约定与测试

### 新增数据源

1. 在 `market/sources/` 或 `financial/sources/` 新增适配器；
2. 行情源必须返回**不复权价**（复权交给 `adjust.py`），成交量单位「股」、成交额「元」、换手率「%」；
3. 在 `service.py` / `loader.py` 注册优先级；
4. 在 `fields.py` 增加字段映射；
5. 补 mocked 单测。

### 新增因子

1. **计算**：在 `factors/ops/`（或 `ops/price_factors.py` 的 `GROUPS`）写纯函数，无 DB/无 IO；
2. **命名**：用 `factors/naming.house_id(category, name)` 生成 ID，格式 `category.semantic[_window]`，
   不复用外部厂商原名；
3. **元数据**：在 `factor_engine/definitions/` 对应文件里生成 `FactorSpec`（分类/子维度/窗口/单位/
   方向/formula/materialization），再在 `definitions/__init__.py::register_all()` 注册；
4. **物化方式**：价量类用 `INLINE`（现算），财务类用 `STORED`（预计算落库，需 `backfill_factors`）；
5. 若是 screener 宽表列，在 `factor_engine/wide.py` 的 `DAILY_COLUMNS` 增加列并实现；
6. 需要筛股字段时，在 `screener/domain/fields.py` / `query_builder.py` 注册；
7. 提升 `formula_version` / factor `version`，让旧值失效并重算；
8. 补离线单测（`tests/test_price_factors.py`、`tests/test_factor_*`），并跑一次
   `python scripts/factor_scorecard.py --only <category>` 看评级与方向是否合理。

### 新增财务字段

1. `fields.py` 增加 canonical 名称与中文口径；
2. `TUSHARE_*_MAP` 增加源字段映射；
3. `scripts/schemas/financial_schema.py` 的 DDL 加列；
4. `python scripts/init_database.py`（存量库需迁移脚本）。

### 测试

```bash
python -m pytest -q               # 离线单测（默认，不联网/不依赖 DB）
RUN_LIVE_TESTS=1 python -m pytest # 额外跑真实数据源/QuestDB 用例
```

当前离线用例覆盖：

- 复权换算与锚点稳定性；
- TTM / 单季不变量、PIT 重述；
- 证券代码（含 92x 北交所）、表结构列一致性；
- 持仓账本（均价/卖出校验/调整持久性）；
- 整合同步的单位换算与空数据日重试；
- 响应信封与序列化；
- Screener 因子、查询 SQL、缺口分类、BuildState 状态机、
  lookback、停牌跳过、查询 limit、北京时间、BaoStock 共享会话。

---

## 13. 因子研究报告与评分卡

### 13.1 它解决什么问题

因子"看起来有信号"和"能用"是两件事。`research/report` 把因子面板 + 行情变成**可评审的报告**
和**可追踪的评分**，回答两个维度：

| 维度 | 问题 | 对应章节 |
|---|---|---|
| **时间** | 信号是否持续（IC 均值 / NW-t）、是否跨周期稳定（分年 / 分市场状态）、衰减多快 | §02 |
| **空间** | 分层是否单调、多空有无经济意义、在市值/行业子样本中是否成立 | §03 |

评价口径固定为**可执行口径**：T 日收盘出信号、T+1 开盘买入、单腿往返 20bp（多空合计 40bp）、非重叠调仓。

### 13.2 单因子报告

```bash
python scripts/factor_report.py --factor momentum.ret_21d --pool 200 --start 2019-01-01
```

产物（只写这一个目录，不产出中间图）：

```
output/factor_reports/<factor_id>.html    # 内联 SVG，A4 打印样式
output/factor_reports/<factor_id>.json    # 评级与关键指标
```

报告结构：摘要（评级徽章 + 4 个 KPI + 方向/局部评级横幅）→ 01 因子定义与样本（公式/单位/
变换/PIT、样本池构造、选样漏斗、幸存者偏差）→ 02 时间维度（IC 时序、分年、衰减、分市场状态 CI、
前后半段一致性）→ 03 空间维度（毛/净分层、净值/回撤/分年、中性化 IC、增量性、买入/卖出端、
成本与容量）→ 04 评级（规则表 + 四象限 + 评分条 + 否决对账 + 结构化断言 + 重估条件）→
05 附录（两类 t 值、方法、局限）。

### 13.3 评分卡（100 分制）

| 分项 | 分值 | 计算 |
|---|---|---|
| 预测力与稳定性 | 45 | 预测力 30 = `30×(0.6×min(1,IC/0.05)+0.4×min(1,ICIR/0.5))`（负值记 0）；稳定性 25 = 分年 CI 显著率 40% + 分状态 30% + 滚动 12m 同号率 30%；两项合维后 ×45/55 |
| 空间单调性 | 15 | 分组**毛收益**每期均值的 Spearman ρ（有符号；IC<0 记 0） |
| 可交易性 | 20 | 覆盖率 6 + 换手 6 + 净价差 t 8 |
| 增量性 | 20 | 同类最高相关 + 剔除同类后的残差 IC（残差为负或相关≥0.95 记 0） |

**一票否决**（任一触发即封顶 C）：

| 规则 | 阈值 |
|---|---|
| 证据不足 | `|IC| < 0.015`（财务 <0.010），或显著年份不足（>=5 年窗口要 3 年，<5 年要 2 年），或方向不一致 |
| 方向与先验相反 | `IC < −0.010` 且独立 t `< −2`；**不自动翻转** |
| 覆盖率不足 | `< 70%` |
| 分层反向 | 单调性 ρ `< 0` |
| 不可交易 | 毛价差 95% 上界 `<` 单期成本 → 触发；下界 `>` 成本 → 通过；否则边界 |
| 换手过高 | **每期**换手 `> 80%`（报告同时给年化换手 `×242/5`） |

评级映射：A ≥ 85 / B ≥ 70 / C ≥ 55 / D < 55。四象限按「时间得分 × 空间得分」划分：
**核心区**（双高）、**过滤因子**（时间高/空间低）、**择时因子**（时间低/空间高）、**淘汰区**。
D 级必须给出失效归因：`无信号(IC→0)` / `方向错` / `复现失败` / `数据不足` / `传导断裂`。

### 13.4 全量跑批与评分卡入库

```bash
# 价量因子（85 个，INLINE 现算）
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --full --render

# 财务因子（61 个，读物化表，先跑 backfill_factors）
python scripts/factor_scorecard.py --source financial --pool 200 --start 2019-01-01 --full --render

# 全库审计：断言违规 + D 级归因 + 20 份家族报告
python scripts/factor_audit.py --dir output/factor_reports

# 相关矩阵 / 家族聚类（只保留代表，不再 20d、21d 各算一个）
python scripts/factor_corr.py --source price

# 方向校准（默认只报告，--apply 才写 registry）
python scripts/calibrate_directions.py --min-abs-ic 0.02
```

评分写入 `factor_scorecard`：

| 字段 | 说明 |
|---|---|
| `ts / run_id / factor_id` | 去重键，同一 run 重跑覆盖，不同 run 保留历史 |
| `grade / score / time_score / space_score` | 评级与分项 |
| `ic_mean / icir / nw_t / ic_win / mono / turnover / coverage` | 关键指标 |
| `direction / calibrated_direction / quadrant / veto` | 设计方向、IC 校准方向、象限、否决原因 |

查询最新一轮：

```sql
select * from factor_scorecard latest on ts partition by factor_id order by score desc
```

### 13.5 注意事项

- **重叠样本看 NW-t，保守判断看独立 t**：报告同时给出 Newey-West t（lag=h−1）与每 h 日取一个观测的独立样本 t；
- **中性化**：逐日横截面回归剔除 log 流通市值与行业哑变量后的残差 IC，原始/市值/行业/双中性并列；
- **增量性实测**：与同类别因子及规模风格做截面相关，再把本因子对全部对照因子回归取残差 IC；
  残差 IC 保留比例低说明大部分信息是已知风格暴露；
- **可交易性拆成买入端/卖出端**：买入端按 T+1 开盘涨停/停牌统计，卖出端按 T+h 收盘跌停/停牌顺延；
  分母/分子逐项列清，旧口径（全部面板单元）与新口径（有信号且有下一日）并列对账；
- **成本按换手率扣除**：`cost = turnover × 单腿往返费率`，不是每次调仓无条件扣 20bp；多空另计两条腿成本（合计 40bp）；
- **时间口径统一北京时间**：报告记录生成时间（UTC+8）、行情截止日、末日覆盖与数据滞后；末日覆盖不足
  前一日 80% 时整日回退，避免用未收盘/未入库 K 线；
- **单调性用每期收益均值**，且用**毛收益**——波动拖累与各组换手差异都会伪造单调性；
- **方向不翻转**：`sig = raw × direction`，方向事先给定；IC 为负记「方向错」并给 D 级归因。
  旧版在同一样本内按 IC 符号翻，会把随机因子的 IC 系统性抬成正数（4/4 强因子 IC>0 即由此而来）；
- **价差断言只在可证边界硬失败**：n=5 时 ρ≥0.8 才必然 Q5>Q1；0<ρ<0.8 可以 Q5<Q1（非单调排列），只报警；
  IC→价差传调用 `IC×σ_5d×2.80` 对照，避免把「IC 显著但价差为负」当正常；
- **不可交易用区间**：毛价差 95% 上界<成本→触发，下界>成本→通过，否则边界；不再有 0.58pp 死区；
- **可成交过滤**默认关闭，`exclude_limit_up=True` 会剔除 T+1 开盘一字涨停/停牌样本；
- **财务因子样本少**（覆盖度选样、2019 起约 70–117 只），D 级不等于因子坏，先看覆盖率与样本期；
  PIT 为 `as_of=available_at` + `lag`，重述未逐期回溯，结论按「上限」读；
- IC 计算已**向量化**（整表 `rank(axis=1)` + 逐行 Pearson），比逐日循环快 35 倍，数值差 1e-16。

---

## 14. 模块文档

[docs/README.md](docs/README.md) 是文档入口：目录结构、全局架构图、实测依赖关系、AI 阅读路径。
交互式系统与数据模型图谱见 [docs/index.html](docs/index.html)。

每篇模块文档固定两节：

1. **架构与依赖**：职责边界、上下游表、关键文件表、数据流；
2. **核心类/方法**：类/函数表（签名 + 用途）、可运行示例、注意事项与坑。

| # | 模块 | 文档 |
|---|---|---|
| 01 | 数据采集编排 | [docs/modules/01-acquisition.md](docs/modules/01-acquisition.md) |
| 02 | 数据访问层 | [docs/modules/02-data-interface.md](docs/modules/02-data-interface.md) |
| 03 | 因子定义与计算 | [docs/modules/03-factors.md](docs/modules/03-factors.md) |
| 04 | 因子物化与注册 | [docs/modules/04-factor-engine.md](docs/modules/04-factor-engine.md) |
| 05 | 因子研究报告 | [docs/modules/05-research.md](docs/modules/05-research.md) |
| 06 | 选股 | [docs/modules/06-screener.md](docs/modules/06-screener.md) |
| 07 | 回测 | [docs/modules/07-backtest.md](docs/modules/07-backtest.md) |
| 08 | 策略 | [docs/modules/08-strategies.md](docs/modules/08-strategies.md) |
| 09 | 持仓 | [docs/modules/09-position.md](docs/modules/09-position.md) |
| 10 | 运维脚本 | [docs/modules/10-scripts.md](docs/modules/10-scripts.md) |
| 11 | MCP 服务器 | [docs/modules/11-mcp-server.md](docs/modules/11-mcp-server.md) |

依赖规则（实测 import 关系）：

```
factors          -> （无内部依赖，纯计算）
strategies       -> factors
data_interface   -> （无硬依赖；acquisition/position 为函数内延迟导入）
factor_engine    -> factors, data_interface
position         -> factors, data_interface
screener         -> factors, factor_engine, data_interface, acquisition
backtest         -> factors, factor_engine, data_interface, strategies
research         -> data_interface（仅 config/timeutil）
acquisition      -> data_interface, factor_engine, screener
```

---

## 数据来源

Tushare、AkShare、BaoStock、腾讯行情。商业使用时请遵守各数据源许可协议。
