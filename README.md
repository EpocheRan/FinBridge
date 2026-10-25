# FinBridge MCP

> 面向 AI Agent 的量化数据与持仓管理服务：行情、三大报表、估值、一致预期、组合账本，统一通过 MCP 工具暴露。

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 5 分钟快速开始](#2-5-分钟快速开始)
- [3. 配置说明](#3-配置说明)
- [4. 数据口径与存储契约](#4-数据口径与存储契约)
- [5. 代码结构](#5-代码结构)
- [6. 数据库设计与迁移](#6-数据库设计与迁移)
- [7. MCP 工具清单](#7-mcp-工具清单)
- [8. 定时任务](#8-定时任务)
- [9. 常见问题](#9-常见问题)
- [10. 开发约定与测试](#10-开发约定与测试)

---

## 1. 项目定位

FinBridge 把量化系统拆成三条清晰的链路：

```text
┌────────────────────────────────────────────────────────────┐
│                        MCP Server                          │
│  行情工具        财务工具         持仓工具 / Skill Prompts  │
└───────┬──────────────┬───────────────────────┬─────────────┘
        │              │                       │
┌───────▼───────┐ ┌────▼────────────────┐ ┌────▼─────────────┐
│ Market Data   │ │ Financial Data      │ │ Position         │
│ 行情/基础信息 │ │ 三大报表/估值/预期  │ │ 组合/持仓/交易   │
└───────┬───────┘ └────┬────────────────┘ └────┬─────────────┘
        │              │                       │
        └──────────────┴───────────┬───────────┘
                                   ▼
                              ┌─────────┐
                              │ QuestDB │
                              └─────────┘
```

| 模块 | 目录 | 职责 |
|---|---|---|
| 数据接口 | `data_interface/` | 行情、财务、交易日历、QuestDB、限流、异常 |
| 持仓 | `position/` | 组合账本、ATR/GRID 仓位、交易与盈亏 |
| 技术指标 | `indicator/` | Backtrader 指标封装 |
| 定时同步 | `scheduled_tasks/` | 全 A 股日线/财务分批同步（心跳驱动） |
| MCP 入口 | `MCPserver.py` | 只组装工具与启动服务 |

**数据源**：Tushare Pro（财务与复权因子主源，至少需要 2000 积分）、AkShare、BaoStock、腾讯实时行情。

---

## 2. 5 分钟快速开始

### 2.1 Docker（推荐）

```bash
cp config.example.json config.json   # 填入 TUSHARE_TOKEN 等
docker compose up -d
```

启动顺序：

1. `questdb` 启动；
2. `db-init` 一次性执行 `scripts/init_database.py`，创建全部表；
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

# 2. 准备配置（config.json 已被 .gitignore 排除，不会进仓库）
cp config.example.json config.json

# 3. 初始化数据库（只需执行一次）
python scripts/init_database.py

# 4. 启动 MCP 服务
python MCPserver.py
```

`config.example.json` 里 `questdb.host = "localhost"`；在 Docker 内则用 `questdb`（程序会自动把宿主机上的 `questdb` 回退成 `localhost`）。

> MCPserver **不会**自动建表。未执行初始化脚本会报"表不存在"。

### 2.3 从旧版本升级（重要）

旧版本的行情表存的是**前复权价**，无法还原成不复权价。升级步骤：

```bash
# 1) 体检：看哪些表要补 adj_factor / 修 amount 类型
python scripts/migrate_database.py --check

# 2) 加列 / 重建 amount 为 DOUBLE
python scripts/migrate_database.py --apply

# 3) 旧行是前复权数据，需要清空后重新同步（推荐）
python scripts/migrate_database.py --apply --truncate-ohlc

# 4) 让 Agent 调 scheduled_sync_a_shares，或等服务自动补齐
```

不想清空历史（接受旧数据被标记为"因子缺失"）时用 `--apply --keep-legacy`，
读取端会返回 `adjust_fallback: "missing_adj_factor"`，不会假装因子是 1。

---

## 3. 配置说明

所有可变配置都在 `config.json`（模板见 `config.example.json`）。**`config.json` 已被 gitignore，请勿提交。**

```json
{
  "questdb": {"host": "localhost", "port": 8812, "rest_port": 9000},
  "tushare": {"enabled": true, "token": "<YOUR_TUSHARE_TOKEN>", "points": 2120},
  "financial": {
    "disclosure_lag_days": {"Q1": 30, "H1": 62, "Q3": 31, "FY": 120}
  },
  "scheduled_tasks": {"include_funds": true, "empty_day_retry_limit": 3},
  "server": {"transport": "http", "host": "0.0.0.0", "port": 8080}
}
```

| 配置段 | 关键项 | 说明 |
|---|---|---|
| `questdb` | `host` / `port` / `rest_port` | PG wire `8812`，HTTP API `9000` |
| `tushare` | `token` / `points` / `requests_per_minute` | 财务与复权因子数据源；至少 2000 积分 |
| `market` | `incremental_source_priority` / `request_timeout_seconds` | 行情增量源顺序：`baostock` / `akshare` / `tushare`（Tushare 仅股票日/周/月线） |
| `financial` | `statement_start_year` / `disclosure_lag_days` | 财报范围与**按报告期区分**的披露滞后 |
| `scheduled_tasks` | `daily_batch_days` / `financial_batch_stocks` / `include_funds` / `empty_day_retry_limit` | 批次大小、是否含 ETF/LOF、空数据日重试上限 |
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

这一节是"数据是否可信"的核心约定。

### 4.1 价格：存不复权价 + 复权因子

- `ohlcv_*` 表只存 **不复权（raw）价**，另存 `adj_factor`（累计后复权因子）。
- 复权在**读取时**计算，除权除息后**不需要回刷历史**：
  - `hfq = price × adj_factor`
  - `qfq = price × adj_factor / anchor`，`anchor` 是该标的**最新**因子（保证同一天在不同查询区间返回一致的前复权价）。
- 因子拿不到（如 ETF 无稳定因子源）时写 `NULL`，读取端返回 `adjust_fallback: "missing_adj_factor"` 且**不做价格变换**。
- 行情增量源可配置 `baostock` / `akshare` / `tushare`。Tushare 仅支持股票日/周/月线，返回不复权价；分钟线仍由 AkShare/BaoStock 提供。

| 场景 | 使用口径 | 原因 |
|---|---|---|
| 行情展示、ATR/止损、技术指标 | `qfq`（默认） | 连续序列，除权日无假跳空 |
| 持仓盈亏、成交价核对 | `none`（不复权） | 成本价是真实成交价，必须同口径 |

### 4.2 单位

| 字段 | 单位 |
|---|---|
| `volume` | 股（东财/腾讯的「手」已 ×100） |
| `amount` | 元（Tushare 千元已 ×1000） |
| `turnover` | %（换手率百分数；新浪的比值已 ×100） |
| 财务金额 | 元（万元/亿元已换算） |
| `dividend_yield` | 小数（3.59% → 0.0359） |

### 4.3 时间

- 统一 **Asia/Shanghai**，写库为 naive 北京时间（与 `trade_date` 语义一致），不依赖容器 TZ。
- 周/月线用 `Period` 作为周期身份，且只生成**已完成**周期，不会写入半根 bar。

### 4.4 财务指标

- 累计报表按 Q1→H1→Q3→FY **相邻报告期**相减拆单季；跳期无法拆分的置 `None` 并标 `GAP`。
- TTM 必须**连续 4 个季度且值非空**，否则返回 `None`（PE/PS/EV 一起为空），不输出虚高数值。
- 估值按 Point-in-Time：每个报告期取 `publish_date <= as_of` 的**最新版本**，财报重述不会让历史季度消失。
- 指标每次都用**全量历史**重算，并做 None 保护（不会把已有同比覆盖成空）。

---

## 5. 代码结构

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
│   │   ├── rate_limiter.py         # 滑动窗口限流
│   │   └── exceptions.py           # 统一异常
│   ├── market/
│   │   ├── adjust.py               # 复权换算（qfq/hfq/none）
│   │   ├── adj_factors.py          # 复权因子提供者（Tushare→AkShare）
│   │   ├── constants.py            # 表名/频率/列顺序
│   │   ├── fetcher.py              # MarketDataFetcher
│   │   ├── loader.py               # MarketDataLoader（增量 + 复权）
│   │   └── sources/                # akshare / baostock / tushare / tencent
│   ├── financial/
│   │   ├── fields.py               # canonical 字段字典（含 PROFILE_COLUMNS）
│   │   ├── normalizer.py           # 字段/单位/派生/校验
│   │   ├── metrics.py              # TTM / EV / 估值倍数（带不变量校验）
│   │   ├── repository.py           # FinancialRepository
│   │   ├── service.py              # FinancialDataService
│   │   └── sources/                # tushare / akshare
│   └── api/
│       ├── runtime.py              # 进程内单例装配（连接/限流只一份）
│       ├── market_tools.py         # 行情类 MCP 工具
│       ├── financial_tools.py      # 财务类 MCP 工具
│       └── utils.py                # 统一响应信封
├── scheduled_tasks/                # 心跳式批量同步
├── position/                       # 组合/持仓/交易 + ATR/GRID 模式
├── indicator/                      # Backtrader 指标
├── scripts/
│   ├── init_database.py            # 建表
│   ├── migrate_database.py         # 存量库迁移（adj_factor / amount 类型）
│   └── schemas/                    # market / financial / position / task
├── tests/                          # 离线单测（默认不联网、不依赖 DB）
└── skills/                         # Agent Skill Prompts
```

---

## 6. 数据库设计与迁移

### 6.1 QuestDB 设计要点

- 每张时序表都有 designated timestamp；
- 按时间分区：`HOUR / DAY / MONTH / YEAR`；
- 行情/财务表使用 `WAL + DEDUP UPSERT KEYS`（同一根 bar / 同一报告期幂等覆盖）；
- **DEDUP key 必须包含 designated timestamp**；
- 持仓表保留原地 `UPDATE` 语义，因此不使用 WAL/DEDUP。

### 6.2 行情表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `ohlcv_1m` ~ `ohlcv_60m` | `ts` | HOUR/DAY | `(ts, code, market)` | 分钟 OHLCV + `adj_factor` |
| `ohlcv_1d` / `1w` / `1mo` | `ts` | MONTH/YEAR | `(ts, code, market)` | 日/周/月 OHLCV、换手率、`adj_factor` |
| `stock_basic_info` | `ts` | MONTH | `(code, ts)` | 基础信息（列集合 = `PROFILE_COLUMNS`） |
| `fund_basic_info` | `ts` | MONTH | `(code, ts)` | ETF/LOF 资料 |
| `concept_boards` / `_ohlc` / `_constituents` | `ts` | YEAR/MONTH | 见 DDL | 概念板块 |
| `trading_days` | `ts` | YEAR | `(ts)` | 交易日历 |

### 6.3 财务表

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
| `fin_raw_report` | `fetched_at` | `(fetched_at, report_id, source)` | 原始报文（审计/重放） |
| `dim_fin_field` / `dim_board` / `map_stock_board` | 见 DDL | — | 字段字典与板块 |

### 6.4 持仓表与任务状态

| 表 | 时间轴 | 说明 |
|---|---|---|
| `portfolios` | `id` | 组合现金、总资产、浮盈/已实现盈亏 |
| `positions` | `id` | 当前持仓、成本、市值、止损、模式参数 |
| `trade_records` | `trade_date` | 买卖/调整流水（软删除） |
| `scheduled_task_state` | `updated_at` | 日线/财务同步游标、状态、进度、pending |

### 6.5 建表与迁移

```bash
python scripts/init_database.py                  # 全部建表
python scripts/init_database.py --only market    # 只建行情表
python scripts/init_database.py --dry-run        # 只打印 DDL

python scripts/migrate_database.py --check       # 存量库体检
python scripts/migrate_database.py --apply       # 加列 / 修类型
python scripts/migrate_database.py --apply --truncate-ohlc   # 清空旧前复权数据
```

> QuestDB 的 `CREATE TABLE IF NOT EXISTS` **不会**给已有表加列，也不会改列类型，
> 所以存量库必须走 `migrate_database.py`。

---

## 7. MCP 工具清单

### 7.1 响应信封（所有工具统一）

```json
{"success": true,  "message": "...", "data": {...}, "warnings": ["..."]}
{"success": false, "message": "...", "error_details": {"type": "...", "suggestion": "..."}}
```

### 7.2 行情

| 工具 | 用途 |
|---|---|
| `get_ohlc_data` | OHLCV（1m/5m/15m/30m/60m/d/w/m），`adjust=none\|qfq\|hfq` |
| `get_ticker_basic_info` | 股票/ETF/LOF 基础信息 |
| `get_realtime_quote` | 腾讯单只实时行情（不复权） |
| `get_trading_days` | 交易日列表 |
| `get_today_date` | 今天日期（北京时间） |
| `get_concept_boards` | 概念板块列表 |

```text
get_ohlc_data(["600519", "000001"], start_date="2024-01-01", end_date="2024-12-31",
              freq="d", adjust="qfq", now=False)
```

返回里带 `adjust` 与 `adjust_fallback`：后者非空表示该标的缺因子、价格按不复权返回。

### 7.3 财务

| 工具 | 用途 |
|---|---|
| `get_company_profile` | 公司资料、交易所/上市板、行业、板块 |
| `get_financial_statements` | IS / BS / CF / ALL，支持 `as_of` 与 `report_type` |
| `get_financial_indicators` | ROE、毛利率、净利率、增长率、偿债指标 |
| `get_valuation` | PE/PB/PS/PCF/PFCF/EV 系列（缺季时为 `null`） |
| `get_estimates` | 收入/净利润/EPS 一致预期 |
| `get_board_membership` | 所属行业/概念/指数 |
| `sync_board_data` | 同步板块目录或成分股 |
| `sync_financial_data` | 主动同步财务数据（按需少量代码） |

### 7.4 持仓

| 工具 | 说明 |
|---|---|
| `pm_create_portfolio` / `pm_list_portfolios` / `pm_modify_portfolio` / `pm_delete_portfolio` | 组合生命周期 |
| `pm_open_position` | 建仓跟踪（`name` 可省略，默认用代码） |
| `pm_execute_trade` | `action` 仅接受 `buy/买入`、`sell/卖出/close`；卖出数量超过持仓会**报错**而不是记成负持仓 |
| `pm_close_position` | 按账本实际数量平仓 |
| `pm_modify_position` | `quantity` 以「调整流水」落库，后续盈亏重算不会覆盖 |
| `pm_check_signals` / `pm_get_buying_advice` | 加减仓信号 / 建仓建议（ATR、止损、风险敞口） |
| `pm_get_portfolio_status` / `pm_list_modes` | 组合状态、可用模式 |

### 7.5 定时任务

| 工具 | 用途 |
|---|---|
| `scheduled_sync_a_shares` | 分批拉全 A 股日线与财务（`dataset=daily\|financial\|all`，`include_funds`） |
| `scheduled_task_status` | 查看游标、进度、待补日期 |

---

## 8. 定时任务

`dataset` 可选 `daily`、`financial`、`all`。每次调用只处理一个有界批次，返回 `next_cursor` 和 `done`；Agent 心跳反复调用直到 `done=true`。

```text
scheduled_sync_a_shares(dataset="daily", start_date="2024-01-01", max_trade_days=20)
scheduled_sync_a_shares(dataset="daily", include_funds=True)
scheduled_sync_a_shares(dataset="financial", start_date="2020-01-01", max_stocks=15)
```

- **日线**：按交易日调 Tushare `daily` + `daily_basic`（换手率）+ `adj_factor`（复权因子），可选 `fund_daily` 拉 ETF/LOF；
- **财务**：按股票池分批，逐只增量拉三大报表、重算指标（可选估值）；
- **空数据日**：当天未收盘导致的"无数据"不算错误，记入 `pending` 下次补；
  历史交易日首次无数据会**暂停在该日**重试，连续 `empty_day_retry_limit` 次才跳过并记 error；
- 游标保存在 `scheduled_task_state`，Agent 重启后可继续。

---

## 9. 常见问题

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

---

## 10. 开发约定与测试

### 新增数据源

1. 在 `market/sources/` 或 `financial/sources/` 新增适配器；
2. 行情源必须返回**不复权价**（复权交给 `adjust.py`），成交量单位「股」、成交额「元」、换手率「%」；
3. 在 `service.py` / `loader.py` 注册优先级；
4. 在 `fields.py` 增加字段映射；
5. 补 mocked 单测。

### 新增财务字段

1. `fields.py` 增加 canonical 名称与中文口径；
2. `TUSHARE_*_MAP` 增加源字段映射；
3. `scripts/schemas/financial_schema.py` 的 DDL 加列；
4. `python scripts/init_database.py`（存量库需迁移脚本）。

### 测试

```bash
uv run pytest                      # 离线单测（默认，不联网/不依赖 DB）
RUN_LIVE_TESTS=1 uv run pytest     # 额外跑真实数据源/QuestDB 用例
```

现有离线用例覆盖：复权换算与锚点稳定性、TTM/单季不变量、PIT 重述、
证券代码（含 92x 北交所）、表结构列一致性、持仓账本（均价/卖出校验/调整持久性）、
定时任务的单位换算与空数据日重试、响应信封与序列化。

### 数据来源

Tushare、AkShare、BaoStock、腾讯行情。商业使用时请遵守各数据源许可协议。
