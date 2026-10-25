# FinBridge MCP

> 面向 AI Agent 的量化数据服务：行情、财务、**因子筛股（Screener）**、组合持仓，统一通过 MCP 工具暴露。

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 5 分钟快速开始](#2-5-分钟快速开始)
- [3. 配置说明](#3-配置说明)
- [4. 数据口径与存储契约](#4-数据口径与存储契约)
- [5. Screener 因子筛股](#5-screener-因子筛股)
- [6. 代码结构](#6-代码结构)
- [7. 数据库设计与迁移](#7-数据库设计与迁移)
- [8. MCP 工具清单](#8-mcp-工具清单)
- [9. 定时任务](#9-定时任务)
- [10. 常见问题](#10-常见问题)
- [11. 开发约定与测试](#11-开发约定与测试)

---

## 1. 项目定位

FinBridge 把量化系统拆成四条链路：

```text
┌──────────────────────────────────────────────────────────────────┐
│                          MCP Server                              │
│  行情工具      财务工具       Screener 工具       持仓 / Skills   │
└───────┬───────────┬───────────────┬──────────────────┬───────────┘
        │           │               │                  │
┌───────▼───────┐ ┌─▼─────────────┐ ┌▼────────────────┐ ┌▼──────────────┐
│ Market Data   │ │ Financial     │ │ Screener        │ │ Position      │
│ 行情/基础信息 │ │ 报表/估值/预期│ │ 日频因子/分时基线│ │ 组合/持仓/交易│
└───────┬───────┘ └─┬─────────────┘ └┬────────────────┘ └┬──────────────┘
        │           │                │                  │
        └───────────┴────────────────┴──────────────────┘
                                    ▼
                              ┌──────────┐
                              │ QuestDB  │
                              └──────────┘
```

| 模块 | 目录 | 职责 |
|---|---|---|
| 数据接口 | `data_interface/` | 行情、财务、交易日历、QuestDB、复权、限流、异常 |
| 因子筛股 | `screener/` | 日频筛选因子、分时量能基线、构建状态、停牌处理 |
| 持仓 | `position/` | 组合账本、ATR/GRID 仓位、交易与盈亏 |
| 技术指标 | `indicator/` | Backtrader 单标的指标封装 |
| 定时同步 | `scheduled_tasks/` | 全 A 股日线/财务分批同步（心跳驱动） |
| MCP 入口 | `MCPserver.py` | 只组装工具与启动服务 |

**数据源**：Tushare Pro（财务与复权因子主源，至少 2000 积分）、AkShare、BaoStock、腾讯实时行情。

**设计原则**：

- 行情表只存**不复权价 + 复权因子**，复权在读取时计算；
- 所有跨模块服务共用 `data_interface/api/runtime.py` 的进程内单例；
- Screener 采用 `interface → application → domain`，基础设施通过端口注入；
- 不在 MCP 启动时建表，建表由 `scripts/init_database.py` 完成。

---

## 2. 5 分钟快速开始

### 2.1 Docker（推荐）

```bash
cp config.example.json config.json   # 填入 TUSHARE_TOKEN 等
docker compose up -d
```

启动顺序：

1. `questdb` 启动；
2. `db-init` 一次性执行 `scripts/init_database.py`，创建全部表（含 screener）；
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

会创建四张表：

- `screener_daily`
- `intraday_baseline`
- `screener_build_state`
- `market_suspension`

### 2.4 从旧版本升级（重要）

旧版本行情表存的是**前复权价**，无法还原成不复权价。升级步骤：

```bash
# 1) 体检：看哪些表要补 adj_factor / 修 amount 类型
python scripts/migrate_database.py --check

# 2) 加列 / 重建 amount 为 DOUBLE
python scripts/migrate_database.py --apply

# 3) 旧行是前复权数据，需要清空后重新同步（推荐）
python scripts/migrate_database.py --apply --truncate-ohlc

# 4) 让 Agent 调 scheduled_sync_a_shares，或等服务自动补齐
```

不想清空历史（接受旧数据被标记为“因子缺失”）时用 `--apply --keep-legacy`，读取端会返回 `adjust_fallback: "missing_adj_factor"`，不会假装因子是 1。

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
  "scheduled_tasks": {"include_funds": true, "empty_day_retry_limit": 3},
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
| `scheduled_tasks` | `daily_batch_days` / `include_funds` / `empty_day_retry_limit` | 批次大小、是否含 ETF/LOF、空数据日重试 |
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

- **日频筛选因子**：涨跌、跳空、量比、均量、ATR、波动率、均线、52 周高低、Beta、市值等；
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
  ScreenerService / BackfillService / BaselineService /
  FactorService / BuildOrchestrator / ports.py
        │
domain
  models / build_state / suspension / fields / factors /
  factor_engine / baseline / coverage / clock
        ▲
        │ 实现端口
infrastructure
  acquisition / repository / query_builder / job_queue / mappers
        │
        └── data_interface（共享行情/QuestDB/交易日历）
```

依赖规则：

- `domain` 不 import `data_interface`、`requests`、`psycopg2`、`backtrader`；
- `application` 只依赖 `ports`，不直接 import infrastructure；
- `infrastructure` 实现端口，可以复用 `data_interface`；
- `bootstrap.py` 是唯一装配具体实现的地方。

### 5.3 表结构

| 表 | 类型 | 说明 |
|---|---|---|
| `screener_daily` | 业务表 | 一只股票一个交易日一行，宽表，直接支撑筛选/排序 |
| `intraday_baseline` | 业务表 | 按 `as_of_date + code + freq + minute_end` 存分时量能基线 |
| `screener_build_state` | 状态表 | 构建/重建状态、进度、游标、质量、租约、重试 |
| `market_suspension` | 状态表 | 停牌 / 退市 / NoData 负缓存 |

关键字段：

- `screener_daily`：
  - 行情：`open/high/low/close/raw_close/adj_factor/prev_close/volume/amount/turnover`
  - 收益：`pct_chg/gap_pct/intraday_pct/range_pct`
  - 量能：`avg_volume_5d/20d/60d`、`avg_amount_20d`、`volume_ratio_20d`、`volume_chg_pct`
  - 波动：`atr14/atr_pct/volatility_20d/volatility_60d`
  - 趋势：`ma5..ma250`、`close_vs_ma20/60/250_pct`
  - 区间：`high_20d/low_20d/high_52w/low_52w`、`dist_*`、`is_new_high_52w/is_new_low_52w`
  - Beta：`benchmark_code/beta_252/corr_252/beta_window`
  - 市值：`total_share/float_share/total_mv/float_mv`
  - 质量：`lookback_days/is_partial/core_ready/full_ready/formula_version/computed_at`
- 查询时派生（不落库）：
  - `price_x_avg_volume = close × avg_volume_20d`
  - `price_x_volume = close × volume`
  - `pct_from_open = (close / open - 1) × 100`
  - `dist_high_20d_pct / dist_low_20d_pct`

> `close` 是前复权价；`raw_close` 是不复权价；`total_mv/float_mv` 使用 `raw_close` 计算。

### 5.4 因子口径

| 因子 | 口径 |
|---|---|
| `pct_chg` | `(close / prev_close - 1) × 100` |
| `gap_pct` | `(open / prev_close - 1) × 100` |
| `range_pct` | `(high - low) / prev_close × 100` |
| `avg_volume_Nd` | 前 N 个交易日成交量均值（不含当日） |
| `volume_ratio_20d` | `volume / avg_volume_20d` |
| `atr14` | Wilder ATR（RMA） |
| `volatility_20d` | 日收益标准差 × `sqrt(252) × 100` |
| `beta_252` | `cov(stock_ret, bench_ret, 252) / var(bench_ret, 252)` |
| `high_52w / low_52w` | 252 个交易日滚动高低点 |
| `full_ready` | 历史长度达到最大窗口且核心因子非空 |
| `is_partial` | `full_ready=false` |

说明：

- 历史不足时仍写行，但 `is_partial=true`；默认查询 `allow_partial=false` 会过滤这些行。
- **Beta 缺失不会把整行标记为 partial**：基准指数是可选交叉因子，基准源不可用时 `beta_252` 为 `null`，但其他因子仍可用。
- 基准默认不阻塞读路径；需要 Beta 时先运行 `python -m screener benchmark`，或把 `screener.backfill.fetch_benchmark` 设为 `true`。

### 5.5 读路径与构建规则

| 场景 | 写 `build_state`？ | 行为 |
|---|---|---|
| 快照 `DONE + FULL + 版本一致 + rows 存在` | 否 | 零写入热路径，直接查询 |
| 快照 `PARTIAL` | 否 | 返回结果 + `quality=PARTIAL`，不自动重算 |
| 未命中 + `auto_build=true` | 是 | `claim` 一条快照状态，内联构建，写 `DONE/PARTIAL` |
| 未命中 + `auto_build=false` | 否 | 抛 `ScreenerNotBuilt` |
| 状态 `RUNNING` 未过期 | 否 | 等待/返回 `BUILDING` |
| 状态 `RUNNING` 已过期 | 是 | 重新 claim，从 cursor 恢复 |
| `rebuild_screener` / CLI / 后台 | 是 | 统一走 `BuildOrchestrator` + inline worker |

构建状态机：

```text
PENDING → RUNNING → DONE
                 ├→ PARTIAL（retry_at 到期后才自动恢复；读路径不重算）
                 ├→ FAILED
                 └→ CANCELLED
```

关键点：

- `build_id` 是确定性幂等键，包含数据集、日期范围、universe 指纹、频率、公式版本；
- `screener_build_state` 采用 append-only，读取用 `LATEST ON updated_at PARTITION BY build_id`；
- `PARTIAL` 的 `retry_at` 只供后台/rebuild 使用，读路径不自动重算，避免抖动。

### 5.6 自动补齐与 lookback

Screener 不要求每天定时跑。三条路径都能补齐：

1. **批量入口**：`python -m screener daily/repair`；
2. **读路径**：`screen_stocks` / `get_screener_metrics` 发现缺口时按预算补齐；
3. **后台 worker**：超过预算转 `InProcessJobDispatcher`。

统一顺序：

```text
DB → 缺口合并 → 第三方源 → 写回 QuestDB → 再从 DB 读取 → 计算 → 写因子表
```

细节：

- 计算 `beta_252` / `52 周` / `ma250` 时自动把 lookback 窗口（默认最多 260 个交易日）带入；
- `max_lookback_bars` 可限制回看上限，超出会截断并告警；
- 停牌/退市/NoData 日期通过 `skip_dates` 排除，不算缺口；
- `NoData` 写 `market_suspension` 并设置 TTL，避免反复打第三方；
- 长区间按 `daily_chunk_years` / `minute_chunk_days` 分块，降低单次请求失败率。

### 5.7 分时量能基线

`intraday_baseline` 的构建逻辑：

1. 取最近 `lookback_days` 个交易日的 1m/5m bar；
2. 只保留常规交易时段（09:30–11:30、13:00–15:00）；
3. 按 `minute_end` 聚合成交量/成交额，并计算累计量；
4. 剔除缺失桶超过 `max_missing_bucket_ratio` 的交易日；
5. 对有效交易日求均值/标准差；
6. `sample_days < min_sample_days` 时 `is_partial=true`。

读取：

```text
time_rel_volume = 当前累计成交量 / avg_cum_volume[当前分钟]
```

### 5.8 CLI

```bash
# 日频因子
python -m screener daily --start 2026-10-01 --end 2026-10-23
python -m screener daily --start 2026-10-23 --end 2026-10-23 --codes 600519

# 修复/强制重建
python -m screener repair --start 2026-01-01 --end 2026-10-23

# 分时基线
python -m screener baseline --as-of 2026-10-23 --codes 600519 --freq 5m

# 补齐基准指数（Beta 需要）
python -m screener benchmark --start 2025-01-01 --end 2026-10-23

# 查看构建状态
python -m screener status
```

### 5.9 MCP 工具

| 工具 | 用途 |
|---|---|
| `screen_stocks` | 按因子筛选/排序股票 |
| `get_screener_metrics` | 查询指定股票的日频因子明细 |
| `get_intraday_baseline` | 查询分时量能基线 |
| `rebuild_screener` | 提交重建任务，返回 `build_id` |
| `get_screener_status` | 查询构建状态/进度 |

示例：

```json
{
  "as_of": "2026-10-23",
  "universe": {"codes": null, "exclude_st": true},
  "filters": [
    {"field": "beta_252", "op": "between", "value": [0.8, 1.5]},
    {"field": "volume_ratio_20d", "op": "ge", "value": 2},
    {"field": "close", "op": "gt", "value": 5}
  ],
  "sort": [{"field": "pct_chg", "desc": true}],
  "limit": 100,
  "auto_build": true,
  "allow_partial": false
}
```

### 5.10 性能与注意事项

- 单日 600519 首次构建约 0.2s，二次命中约 0.02s（本地 QuestDB，实测）；
- 全市场首次回填受第三方源限速影响，建议用 CLI/后台跑，不要在 MCP 请求里同步等待；
- 默认 `allow_partial=false`，历史不足的股票会被过滤；需要包含时显式传 `true`；
- 默认 `fetch_benchmark=false`，Beta 需要先跑 `screener benchmark`；
- 读路径默认预算 2s，超时转后台 worker；V1 只支持 inline 调度。

---

## 6. 代码结构

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
│   └── api/
│       ├── runtime.py              # 进程内单例装配
│       ├── market_tools.py         # 行情 MCP 工具
│       ├── financial_tools.py      # 财务 MCP 工具
│       └── utils.py                # 统一响应信封
├── screener/
│   ├── bootstrap.py                # composition root
│   ├── config.py
│   ├── domain/                     # 纯模型/公式/状态机
│   ├── application/                # 用例编排 + ports
│   ├── infrastructure/             # QuestDB 仓储/补齐/SQL/队列
│   └── interface/                  # MCP tools / CLI / worker_main
├── scheduled_tasks/                # 心跳式批量同步
├── position/                       # 组合/持仓/交易 + ATR/GRID
├── indicator/                      # Backtrader 指标
├── scripts/
│   ├── init_database.py            # 建表
│   ├── migrate_database.py         # 存量库迁移
│   └── schemas/                    # market/financial/position/screener/task
├── tests/                          # 离线单测（默认不联网、不依赖 DB）
└── skills/                         # Agent Skill Prompts
```

---

## 7. 数据库设计与迁移

### 7.1 QuestDB 设计要点

- 每张时序表都有 designated timestamp；
- 按时间分区：`HOUR / DAY / MONTH / YEAR`；
- 行情/财务/Screener 表使用 `WAL + DEDUP UPSERT KEYS`；
- **DEDUP key 必须包含 designated timestamp**；
- 持仓表保留原地 `UPDATE` 语义，因此不使用 WAL/DEDUP。

### 7.2 行情表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `ohlcv_1m` ~ `ohlcv_60m` | `ts` | HOUR/DAY | `(ts, code, market)` | 分钟 OHLCV + `adj_factor` |
| `ohlcv_1d` / `1w` / `1mo` | `ts` | MONTH/YEAR | `(ts, code, market)` | 日/周/月 OHLCV、换手率、`adj_factor` |
| `stock_basic_info` | `ts` | MONTH | `(code, ts)` | 基础信息（`PROFILE_COLUMNS`） |
| `fund_basic_info` | `ts` | MONTH | `(code, ts)` | ETF/LOF 资料 |
| `concept_boards` / `_ohlc` / `_constituents` | `ts` | YEAR/MONTH | 见 DDL | 概念板块 |
| `trading_days` | `ts` | YEAR | `(ts)` | 交易日历 |

### 7.3 财务表

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

### 7.4 Screener 表

| 表 | 时间轴 | 分区 | DEDUP KEY | 说明 |
|---|---|---|---|---|
| `screener_daily` | `ts` | MONTH | `(ts, code, market)` | 日频筛选因子 |
| `intraday_baseline` | `as_of_date` | MONTH | `(as_of_date, code, market, freq, minute_end)` | 分时量能基线 |
| `screener_build_state` | `updated_at` | YEAR | `(updated_at, build_id)` | 构建状态（append-only） |
| `market_suspension` | `updated_at` | YEAR | `(updated_at, code, market, status, start_date)` | 停牌/退市/NoData |

### 7.5 持仓表与任务状态

| 表 | 时间轴 | 说明 |
|---|---|---|
| `portfolios` | `id` | 组合现金、总资产、浮盈/已实现盈亏 |
| `positions` | `id` | 当前持仓、成本、市值、止损、模式参数 |
| `trade_records` | `trade_date` | 买卖/调整流水（软删除） |
| `scheduled_task_state` | `updated_at` | 日线/财务同步游标、状态、进度、pending |

### 7.6 建表与迁移

```bash
python scripts/init_database.py                  # 全部建表
python scripts/init_database.py --only market    # 只建行情表
python scripts/init_database.py --only screener  # 只建 Screener 表
python scripts/init_database.py --dry-run        # 只打印 DDL

python scripts/migrate_database.py --check       # 存量库体检
python scripts/migrate_database.py --apply       # 加列 / 修类型
python scripts/migrate_database.py --apply --truncate-ohlc   # 清空旧前复权数据
```

> QuestDB 的 `CREATE TABLE IF NOT EXISTS` **不会**给已有表加列，也不会改列类型，
> 所以存量库必须走 `migrate_database.py`。Screener 是新增表，`--only screener` 即可。

---

## 8. MCP 工具清单

### 8.1 响应信封（所有工具统一）

```json
{"success": true,  "message": "...", "result": {...}}
{"success": false, "message": "...", "error_details": {"type": "...", "suggestion": "..."}}
```

### 8.2 行情

| 工具 | 用途 |
|---|---|
| `get_ohlc_data` | OHLCV（1m/5m/15m/30m/60m/d/w/m），`adjust=none\|qfq\|hfq` |
| `get_ticker_basic_info` | 股票/ETF/LOF 基础信息 |
| `get_realtime_quote` | 腾讯单只实时行情（不复权） |
| `get_trading_days` | 交易日列表 |
| `get_today_date` | 今天日期（北京时间） |
| `get_concept_boards` | 概念板块列表 |

### 8.3 财务

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

### 8.4 Screener

| 工具 | 用途 |
|---|---|
| `screen_stocks` | 因子筛选/排序（支持 `spec` 或扁平参数） |
| `get_screener_metrics` | 单只/多只股票的日频因子明细 |
| `get_intraday_baseline` | 分时量能基线 |
| `rebuild_screener` | 提交重建任务（`screener_daily` / `intraday_baseline`） |
| `get_screener_status` | 构建状态/进度 |

### 8.5 持仓

| 工具 | 说明 |
|---|---|
| `pm_create_portfolio` / `pm_list_portfolios` / `pm_modify_portfolio` / `pm_delete_portfolio` | 组合生命周期 |
| `pm_open_position` | 建仓跟踪（`name` 可省略，默认用代码） |
| `pm_execute_trade` | `action` 仅接受 `buy/买入`、`sell/卖出/close`；卖出超过持仓会报错 |
| `pm_close_position` | 按账本实际数量平仓 |
| `pm_modify_position` | `quantity` 以「调整流水」落库 |
| `pm_check_signals` / `pm_get_buying_advice` | 加减仓信号 / 建仓建议 |
| `pm_get_portfolio_status` / `pm_list_modes` | 组合状态、可用模式 |

### 8.6 定时任务

| 工具 | 用途 |
|---|---|
| `scheduled_sync_a_shares` | 分批拉全 A 股日线与财务（`dataset=daily\|financial\|all`） |
| `scheduled_task_status` | 查看游标、进度、待补日期 |

---

## 9. 定时任务

`dataset` 可选 `daily`、`financial`、`all`。每次调用只处理一个有界批次，返回 `next_cursor` 和 `done`；Agent 心跳反复调用直到 `done=true`。

```text
scheduled_sync_a_shares(dataset="daily", start_date="2024-01-01", max_trade_days=20)
scheduled_sync_a_shares(dataset="daily", include_funds=True)
scheduled_sync_a_shares(dataset="financial", start_date="2020-01-01", max_stocks=15)
```

- **日线**：按交易日调 Tushare `daily` + `daily_basic`（换手率）+ `adj_factor`（复权因子），可选 `fund_daily` 拉 ETF/LOF；
- **财务**：按股票池分批，逐只增量拉三大报表、重算指标（可选估值）；
- **空数据日**：当天未收盘导致的“无数据”不算错误，记入 `pending` 下次补；
  历史交易日首次无数据会暂停在该日重试，连续 `empty_day_retry_limit` 次才跳过并记 error；
- 游标保存在 `scheduled_task_state`，Agent 重启后可继续。

Screener 不依赖定时任务：批量 CLI、读路径和 inline worker 都能补齐。

---

## 10. 常见问题

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

## 11. 开发约定与测试

### 新增数据源

1. 在 `market/sources/` 或 `financial/sources/` 新增适配器；
2. 行情源必须返回**不复权价**（复权交给 `adjust.py`），成交量单位「股」、成交额「元」、换手率「%」；
3. 在 `service.py` / `loader.py` 注册优先级；
4. 在 `fields.py` 增加字段映射；
5. 补 mocked 单测。

### 新增 Screener 因子

1. 在 `screener/domain/factors.py` 增加纯函数；
2. 在 `screener/domain/factor_engine.py` 组合输出；
3. 在 `scripts/schemas/screener_schema.py` 加列，并更新 `mappers.py`；
4. 在 `screener/domain/fields.py` 注册可筛/可排字段；
5. 在 `screener/domain/models.py` / `query_builder.py` 需要时增加派生表达式；
6. 提升 `formula_version`，让旧行自动失效并重算；
7. 补离线单测。

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
- 定时任务的单位换算与空数据日重试；
- 响应信封与序列化；
- Screener 因子、查询 SQL、缺口分类、BuildState 状态机、
  lookback、停牌跳过、查询 limit、北京时间、BaoStock 共享会话。

---

## 数据来源

Tushare、AkShare、BaoStock、腾讯行情。商业使用时请遵守各数据源许可协议。
