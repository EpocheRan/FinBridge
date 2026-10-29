# 02 · data_interface（数据访问层）

## 1. 架构与依赖

### 1.1 职责边界

`data_interface` 是**所有 QuestDB 读写的唯一入口**，向上提供业务语义（行情、财务、资金流、
板块、披露），向下屏蔽数据源差异（Tushare/BaoStock/东财/同花顺/腾讯/新浪）。

它做四件事：

1. **取数**：按源优先级拉取，失败降级；
2. **归一化**：字段名、单位、复权、时区、代码格式统一；
3. **入库**：`DEDUP UPSERT` 写长表/宽表；
4. **PIT 语义**：财务按 `publish_date` 对齐，避免未来函数。

它**不**算因子（→ `factor_engine`）、不做选股、不管持仓。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | QuestDB、外部数据源 | 唯一存储；HTTP/SDK 取数 |
| 下游 | `factor_engine` | 读 `ohlcv_1d` / `fin_*` / `moneyflow_daily` |
| 下游 | `screener`、`backtest`、`research`、`position` | 读行情/财务 |
| 被调用 | `acquisition` | 调 `sync_*` 写数据 |
| 延迟依赖 | `acquisition`、`position` | 只在 `api/runtime.py` **函数体内** import，避免循环 |

### 1.3 关键文件

| 子包 | 文件 | 作用 |
|---|---|---|
| `common/` | `questdb_client.py` | PG 连接、`query_dataframe`、`execute_*`、`sql_literal` |
| | `config.py` | `get_config()`、`resolve_questdb_host()`、`project_path()` |
| | `timeutil.py` | `now_cn()`、`today_str()`、`trading_session()`、时区工具 |
| | `security_code.py` | `normalize_code`/`exchange_of`/`board_of`/`is_bj_code` |
| | `trading_calendar.py` | `TradingCalendar`、`iter_trading_days` |
| | `rate_limiter.py` / `exceptions.py` / `progress.py` | 限流、异常体系、进度上报 |
| `market/` | `loader.py` | `MarketDataLoader`：增量判断 + 复权 + 实时补尾 |
| | `fetcher.py` | `MarketDataFetcher`：多源调度与写入 |
| | `sources/*.py` | tushare / baostock / akshare / sina / tencent / index |
| | `adjust.py` / `adj_factors.py` | 复权因子应用 |
| | `intraday.py` | 分时指标：涨跌停价、封板时间、VWAP 偏离 |
| `financial/` | `service.py` | `FinancialDataService`：报表/指标/估值同步与 PIT 查询 |
| | `repository.py` | SQL 读写 |
| | `normalizer.py` / `metrics.py` | 字段归一、季度化、TTM |
| `moneyflow/` | `service.py` | 资金流同步/排名 |
| `concept/` | `service.py` | 板块目录、成分、板块日线、个股↔板块映射 |
| `disclosure/` | `service.py` | 财报披露日历、公告 |
| `api/` | `*_tools.py` / `runtime.py` / `utils.py` | MCP 工具注册、单例管理、JSON 序列化 |

### 1.4 数据流（以日线为例）

```
MarketDataLoader.ohlc(codes, start, end, freq, adjust)
   ├─ 读 QuestDB 已有数据（判断缺口：交易日历 vs 实际日期）
   ├─ 缺口 → MarketDataFetcher.get_ohlc → 源优先级（tushare → akshare → baostock）
   ├─ 复权：adj_factor → apply_adjust
   ├─ 当天 → 追加实时行情（可关闭）
   └─ 写回 ohlcv_1d（DEDUP UPSERT）
```

## 2. 核心类与方法

### 2.1 类表

| 类 | 关键方法 | 说明 |
|---|---|---|
| `QuestDBClient` | `query_dataframe(sql)` / `execute(sql)` / `execute_values(prefix, rows)` / `execute_ddl(sql)` | 所有 SQL 的入口 |
| `MarketDataLoader` | `ohlc(codes, start, end, freq, adjust, source_priority)` / `ticker_basic_info` / `concept_boards*` | 行情主入口，含增量与复权 |
| `MarketDataFetcher` | `get_ohlc(...)` / `get_realtime_quotes` / `update_ohlc(data, freq)` | 多源调度 + 写库 |
| `FinancialDataService` | `get_statements` / `get_valuation` / `get_indicators` / `sync_*` / `ensure_stock_universe` | 财务 PIT |
| `MoneyflowService` | `get_by_code` / `get_by_date` / `rank` / `sync` | 资金流 |
| `ConceptService` | `sync_boards` / `stock_boards(code)` / `compute_board_daily` / `sync_stock_boards` | 板块 |
| `DisclosureService` | `calendar(code)` / `upcoming(days)` / `announcements(code)` | 披露 |
| `TradingCalendar` | `is_trading_day` / `trading_days(start, end)` | 交易日历 |

### 2.2 用法

```python
from data_interface import MarketDataLoader, FinancialDataService
from data_interface.common.questdb_client import QuestDBClient

# 行情（后复权，自动补缺口）
loader = MarketDataLoader()
df = loader.ohlc(["600519"], "2026-01-01", "2026-10-28", freq="1d", adjust="hfq")

# 财务（PIT：只返回 as_of 之前已披露的报表）
fin = FinancialDataService()
stmts = fin.get_statements("600519", "IS", as_of="2026-10-28")

# 直接 SQL
db = QuestDBClient()
frame = db.query_dataframe("select * from ohlcv_1d where code='600519' limit 10")
```

MCP 工具：`data_interface/api/market_tools.py` 等注册 `get_ohlc`、`get_financial_statements`、
`get_stock_boards`、`get_disclosure_calendar` 等（见 11-mcp-server）。

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **时区** | 库里存的是北京时间但**标签是 UTC**。读出后 `.dt.tz_localize(None)` 剥离，**不要 `tz_convert`**，否则整体偏移 8 小时 |
| **WAL 可见性** | 写入后立刻读可能读不到；需要重试（`concept/repository.py` 有 `_wait_for_boards` 式实现） |
| **不支持 DELETE** | 只能 `UPDATE`（异步生效）或整分区重建 |
| **`LATEST ON ... PARTITION BY` 后不能接 WHERE** | 必须包一层子查询：`select * from (select ... latest on ...) where ...` |
| **北交所代码** | `43/83/87/92` 开头，判定必须**放在沪市之前**，否则被误判为 `sh` |
| **新浪分时上限** | 单次 1023 根：1 分钟≈4 天，5 分钟≈21 天，必须分段请求 |
| **BaoStock 会挂** | 5 分钟线曾卡死 >5 分钟；已加 20s 超时 + 熔断，`sina` 优先 |
| **财务是 PIT** | `get_statements(as_of=...)` 用 `publish_date` 过滤；绕过它会引入未来函数 |
| **`fin_indicator.net_income`** | 是**净利润**（含少数股东），不是归母；归母用 `net_income_attributable_to_parent` |
| **资金流字段** | `net_mf_amount` ≠ 主力净额；主力 = `buy_elg + buy_lg − sell_elg − sell_lg` |
| **大表查询** | `factor_value_daily` / `moneyflow_daily` 必须带 `ts` 范围，否则全表扫 |
| **代码归一** | 统一用 `normalize_code()`，不要自己 `zfill`/拼后缀 |
