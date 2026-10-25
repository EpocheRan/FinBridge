# FinBridge MCP

> 面向 AI Agent 的量化数据与持仓管理服务：行情、三大报表、估值、一致预期、组合账本，统一通过 MCP 工具暴露。

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 5 分钟快速开始](#2-5-分钟快速开始)
- [3. 配置说明](#3-配置说明)
- [4. 代码结构](#4-代码结构)
- [5. 数据库设计](#5-数据库设计)
- [6. 数据库初始化](#6-数据库初始化)
- [7. MCP 工具清单](#7-mcp-工具清单)
- [8. 常见问题](#8-常见问题)
- [9. 开发约定](#9-开发约定)

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
| MCP 入口 | `MCPserver.py` | 只组装工具与启动服务 |

**数据源**：Tushare Pro（财务主源，至少需要 2000 积分）、AkShare、BaoStock、腾讯实时行情。

---

## 2. 5 分钟快速开始

### 2.1 Docker（推荐）

```bash
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

> 首次启动 `mcp-app` 会在容器内安装 Python 依赖（`uv sync`），网络较慢时可能需要几分钟；依赖保存在 `mcp_venv` 命名卷中，后续重启不会重复下载。看到 `Starting MCP server` 即表示就绪。

如果修改了表结构，重新初始化：

```bash
docker compose run --rm db-init
```

> 如果提示容器名已存在（例如旧的 `questdb-server`），先清理：
>
> ```bash
> docker rm -f questdb-server mcp-app mcp-db-init
> docker compose up -d
> ```

### 2.2 本地运行（非 Docker）

前提：QuestDB 已启动并监听 `8812`。

```bash
# 1. 安装依赖
uv sync

# 2. 初始化数据库（只需执行一次）
python scripts/init_database.py

# 3. 启动 MCP 服务
python MCPserver.py
```

`config.json` 默认 `questdb.host = "questdb"`。本地执行脚本时，程序会自动回退到 `localhost`；也可以手动改成 `localhost`。

> MCPserver **不会**自动建表。未执行初始化脚本会报“表不存在”。

---

## 3. 配置说明

所有可变配置都在 `config.json`。

```json
{
  "questdb": {
    "host": "questdb",
    "port": 8812,
    "user": "admin",
    "password": "quest",
    "database": "stocks",
    "rest_port": 9000
  },
  "tushare": {
    "enabled": true,
    "token": "<YOUR_TUSHARE_TOKEN>",
    "points": 2000,
    "requests_per_minute": 200,
    "report_rc_daily_limit": 10
  },
  "financial": {
    "statement_start_year": 2000,
    "valuation_start_year": 2020,
    "estimate_enabled": true
  }
}
```

| 配置段 | 关键项 | 说明 |
|---|---|---|
| `questdb` | `host` / `port` / `rest_port` | PG wire `8812`，HTTP API `9000` |
| `tushare` | `token` / `points` / `requests_per_minute` | 财务数据源配置；至少需要 2000 积分 |
| `market` | `incremental_source_priority` / `request_timeout_seconds` | 行情增量源顺序 |
| `financial` | `statement_start_year` / `valuation_start_year` / `estimate_enabled` | 财报与估值同步范围 |
| `server` | `transport` / `host` / `port` | 默认 SSE `8080` |
| `logging` | `directory` / `file` / `max_bytes` / `backup_count` | 日志轮转 |

> `config.json` 包含 Tushare token，请勿提交到公共仓库。生产环境建议用挂载文件或密钥管理覆盖。

### 3.1 数据源网址

| 数据源 | 用途 | 网址 |
|---|---|---|
| Tushare | 财报、估值、一致预期 | https://tushare.pro/ |
| AkShare | 行情、基础信息、板块、预期兜底 | https://akshare.akfamily.xyz/ |
| BaoStock | 行情备用、交易日历 | http://baostock.com/ |
| 腾讯行情 | 单只实时行情 | https://gu.qq.com/ |
| QuestDB | 时序数据库 | https://questdb.io/ |

> Tushare 至少需要 2000 积分；token 与积分请在 Tushare 官网配置。

### 3.2 限流行为

- 主动限流：按 `tushare.requests_per_minute` 控制请求频率；
- 服务端分钟限流：自动等待重试，耗尽后抛 `RateLimitExceededError`；
- 每日总量超限：抛 `DailyQuotaExceededError`；
- 权限/积分不足：抛 `PermissionDeniedError`；
- `report_rc`：本地配额文件 + 独立限流器保护，超限后自动降级 AkShare 盈利预测。

MCP 工具会把这些异常转成结构化错误：

```json
{
  "success": false,
  "error_details": {
    "type": "RateLimitExceededError",
    "interface": "income",
    "retry_after_seconds": 60
  }
}
```

---

## 4. 代码结构

```text
FinBridge/
├── MCPserver.py                    # MCP 入口：注册工具 + 启动
├── config.json                     # 全部运行时配置
├── data_interface/                 # “数据接口”层
│   ├── common/
│   │   ├── config.py               # 配置加载
│   │   ├── questdb_client.py       # QuestDBClient
│   │   ├── baostock_client.py      # BaoStockClient
│   │   ├── trading_calendar.py     # TradingCalendar
│   │   ├── rate_limiter.py         # 滑动窗口限流
│   │   └── exceptions.py           # 统一异常
│   ├── market/
│   │   ├── constants.py            # 表名/频率
│   │   ├── fetcher.py              # MarketDataFetcher
│   │   ├── loader.py               # MarketDataLoader
│   │   └── sources/
│   │       ├── akshare_source.py   # AkshareMarketSource
│   │       ├── baostock_source.py  # BaoStockMarketSource
│   │       └── tencent_source.py   # TencentQuoteSource
│   ├── financial/
│   │   ├── fields.py               # canonical 字段字典
│   │   ├── normalizer.py           # 字段/单位/派生/校验
│   │   ├── repository.py           # FinancialRepository
│   │   ├── service.py              # FinancialDataService
│   │   ├── metrics.py              # TTM / EV / 估值倍数
│   │   └── sources/
│   │       ├── tushare_source.py   # TushareSource
│   │       └── akshare_source.py   # AkShareFinancialSource
│   └── api/
│       ├── market_tools.py         # 行情类 MCP 工具
│       ├── financial_tools.py      # 财务类 MCP 工具
│       └── utils.py                # 序列化/错误响应
├── position/
│   ├── manager.py                  # PositionManager
│   ├── llm_tool.py                 # PositionAgentInterface
│   ├── schema.py                   # 组合/持仓/交易 ORM
│   └── modes/                      # ATR / GRID
├── indicator/                      # Backtrader 指标
├── scripts/
│   ├── init_database.py            # 初始化主脚本
│   └── schemas/
│       ├── market_schema.py
│       ├── financial_schema.py
│       └── position_schema.py
├── skills/                         # Agent Skill Prompts
└── tests/
```

**命名约定**：

| 旧 | 新 |
|---|---|
| `DataLoader` | `MarketDataLoader` |
| `DataFetcher` | `MarketDataFetcher` |
| `questDB` | `QuestDBClient` |
| `BS` | `BaoStockClient` |
| `TradingDateManage` | `TradingCalendar` |
| `init_market_data_db.py` | `scripts/schemas/market_schema.py` |

---

## 5. 数据库设计

### 5.1 QuestDB 设计要点

- 每张时序表都有 designated timestamp；
- 按时间分区：`HOUR / DAY / MONTH / YEAR`；
- 行情/财务表使用 `WAL + DEDUP UPSERT KEYS`；
- **DEDUP key 必须包含 designated timestamp**（QuestDB 的硬性约束）；
- 持仓表保留原地 `UPDATE` 语义，因此不使用 WAL/DEDUP。

### 5.2 行情表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `ohlcv_1m` ~ `ohlcv_60m` | `ts` | HOUR/DAY | `(ts, code, market)` | 分钟级 OHLCV 与成交额 |
| `ohlcv_1d` / `1w` / `1mo` | `ts` | MONTH/YEAR | `(ts, code, market)` | 日/周/月 OHLCV、成交额、换手率 |
| `stock_basic_info` | `ts` | MONTH | `(code, ts)` | 股票基础信息、上市板、行业、股本、市值 |
| `fund_basic_info` | `ts` | MONTH | `(code, ts)` | ETF/LOF 基金资料 |
| `concept_boards` | `ts` | YEAR | `(ts, code)` | 概念板块目录 |
| `concept_boards_ohlc` | `ts` | MONTH | `(ts, code)` | 概念板块指数行情 |
| `concept_constituents` | `ts` | YEAR | `(ts, board_code, stock_code)` | 概念板块成分股 |
| `trading_days` | `ts` | YEAR | `(ts)` | 交易日历（是否交易日） |

### 5.3 财务表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `fin_report_meta` | `period_end` | YEAR | `(period_end, report_id)` | 报表版本、报告期、披露日、质量状态 |
| `fin_income_statement` | `period_end` | YEAR | `(period_end, report_id)` | 利润表：收入、成本、费用、EBIT/EBITDA、净利润、EPS |
| `fin_balance_sheet` | `period_end` | YEAR | `(period_end, report_id)` | 资产负债表：资产、负债、债务、权益、商誉、现金 |
| `fin_cash_flow` | `period_end` | YEAR | `(period_end, report_id)` | 现金流量表：经营/投资/筹资现金流、CapEx、FCF |
| `fin_estimate` | `as_of` | MONTH | `(code, metric, fiscal_year, fiscal_period, as_of, source)` | 卖方一致预期：收入、净利润、EPS 等 |
| `fin_valuation_daily` | `ts` | MONTH | `(ts, code, source)` | 每日估值：市值、EV、PE/PB/PS/PCF/PFCF、EV 系列 |
| `fin_sync_state` | `updated_at` | YEAR | `(updated_at, code, dataset)` | 各数据集最后同步时间、状态与错误信息 |
| `fin_raw_report` | `fetched_at` | YEAR | `(fetched_at, report_id, source)` | 原始报表字段快照，用于审计/重放 |
| `dim_fin_field` | `updated_at` | YEAR | `(updated_at, canonical_name, statement_type)` | 字段字典：中文名、单位、公式、源字段别名 |
| `dim_board` | `ts` | YEAR | `(board_id, board_type, source, ts)` | 板块字典：行业、概念、指数 |
| `map_stock_board` | `ts` | YEAR | `(code, board_id, ts)` | 股票与板块的多对多归属关系 |

### 5.4 持仓表

| 表 | 时间轴 | 说明 |
|---|---|---|
| `portfolios` | `id` | 组合现金、总资产、浮盈/已实现盈亏 |
| `positions` | `id` | 当前持仓、成本、市值、止损、模式参数 |
| `trade_records` | `trade_date` | 买卖流水（软删除） |

---

## 6. 数据库初始化

### 6.1 主脚本

```bash
# 全部初始化
python scripts/init_database.py

# 只初始化行情 + 财务
python scripts/init_database.py --only market,financial

# 只打印 DDL，不连接数据库
python scripts/init_database.py --dry-run

# 覆盖连接
python scripts/init_database.py --host 127.0.0.1 --port 8812
```

主脚本会自动判断：

- 操作系统：Windows / Linux / macOS；
- 是否在 Docker：`/.dockerenv` 或 `RUNNING_IN_DOCKER=1`；
- `questdb.host=questdb` 且不在 Docker：自动回退 `localhost`；
- 连接失败：按 `connect_retries` 重试并给出提示。

### 6.2 子脚本

| 子脚本 | 创建内容 |
|---|---|
| `schemas/market_schema.py` | 多频率 OHLCV、基础信息、基金、概念板块、交易日历 |
| `schemas/financial_schema.py` | 三大报表、估值、一致预期、同步状态、原始报文、字段字典、板块 |
| `schemas/position_schema.py` | 组合、持仓、交易流水 |

### 6.3 Docker 初始化

`docker-compose.yml` 中的 `db-init` 服务会执行主脚本，成功后退出；`mcp-server` 依赖它完成后再启动。

---

## 7. MCP 工具清单

### 7.1 行情

| 工具 | 用途 |
|---|---|
| `get_ohlc_data` | OHLCV（1m/5m/15m/30m/60m/d/w/m） |
| `get_ticker_basic_info` | 股票/ETF/LOF 基础信息 |
| `get_realtime_quote` | 腾讯单只实时行情 |
| `get_trading_days` | 交易日列表 |
| `get_today_date` | 今天日期 |
| `get_concept_boards` | 概念板块列表 |

### 7.2 财务

| 工具 | 用途 |
|---|---|
| `get_company_profile` | 公司资料、交易所/上市板、行业、板块 |
| `get_financial_statements` | 利润表 / 资产负债表 / 现金流量表 |
| `get_financial_indicators` | ROE、毛利率、净利率、增长率、偿债指标 |
| `get_valuation` | PE/PB/PS/PCF/PFCF/EV 系列 |
| `get_estimates` | 收入/净利润/EPS 一致预期 |
| `get_board_membership` | 股票所属行业/概念/指数 |
| `sync_board_data` | 同步板块目录或成分股 |
| `sync_financial_data` | 主动同步财务数据（按需调用） |

### 7.3 持仓

`pm_*` 工具：创建组合、交易、平仓、修改持仓、检查信号、买入建议、查询组合状态。

---



---

## 8. 常见问题

### Q1: `Conflict. The container name "/questdb-server" is already in use`

```bash
docker rm -f questdb-server mcp-app mcp-db-init
docker compose up -d
```

### Q2: `QuestDB 未连接` / `Connection refused`

检查：

```bash
docker ps
Test-NetConnection localhost -Port 8812
```

本地运行时确认 `config.json` 的 host/port；脚本会自动把 `questdb` 回退到 `localhost`。

### Q3: `表不存在`

MCPserver 不再自动建表。执行：

```bash
python scripts/init_database.py
```

### Q4: Tushare 报 `RateLimitExceededError`

- 降低同步频率，或等待 `retry_after_seconds` 后重试；
- 查看错误中的 `interface`、`limit` 字段定位具体接口。

### Q5: Tushare 报 `PermissionDeniedError`

检查 `config.json` 的 `tushare.token` 和 `points`。Tushare 至少需要 2000 积分。

### Q6: AkShare 网页接口失败

AkShare 是网页接口封装，可能被限流/断连。基础信息会优先用 Tushare `stock_basic` + `daily_basic`，AkShare 仅作补充。

---

## 9. 开发约定

### 新增数据源

1. 在 `data_interface/financial/sources/` 或 `market/sources/` 新增适配器；
2. 返回 canonical 字段或原始行；
3. 在 `service.py` 注册优先级；
4. 在 `fields.py` 增加字段映射；
5. 补充限流与异常处理。

### 新增财务字段

1. 在 `fields.py` 增加 canonical 名称与中文口径；
2. 在 `TUSHARE_*_MAP` 增加源字段映射；
3. 在 `scripts/schemas/financial_schema.py` 的对应 DDL 加列；
4. 执行 `python scripts/init_database.py`（QuestDB 的 `CREATE TABLE IF NOT EXISTS` 不会自动加列，需要迁移脚本或重建表）。

### 数据来源

Tushare、AkShare、BaoStock、腾讯行情。商业使用时请遵守各数据源许可协议。
