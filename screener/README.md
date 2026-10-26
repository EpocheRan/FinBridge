# Screener 模块

日频筛选因子 + 分时量能基线，按四层组织：

```
interface  -> api.py / cli.py / worker_main.py
application -> ScreenerService / BackfillService / BaselineService /
               FactorService / BuildOrchestrator / ports.py
domain     -> models / build_state / suspension / fields / factors /
               factor_engine / baseline / coverage
infrastructure -> acquisition / repository / query_builder / job_queue / mappers
bootstrap.py -> composition root
```

`data_interface` 是共享数据访问实现，逻辑上属于 infrastructure，物理位置保持不变。

## 表

- `screener_daily`：日频筛选因子
- `intraday_baseline`：分时量能基线
- `screener_build_state`：构建状态、断点续跑、幂等键
- `market_suspension`：停牌/退市/NoData 负缓存

初始化：

```bash
python scripts/init_database.py --only screener
```

## 使用

MCP 工具：

- `screen_stocks`
- `get_screener_metrics`
- `get_intraday_baseline`
- `rebuild_screener`
- `get_screener_status`

CLI：

```bash
python -m screener daily --start 2026-10-01 --end 2026-10-23
python -m screener daily --start 2026-10-23 --end 2026-10-23 --codes 600519
python -m screener repair --start 2026-01-01 --end 2026-10-23
python -m screener baseline --as-of 2026-10-23 --codes 600519 --freq 5m
python -m screener benchmark --start 2025-01-01 --end 2026-10-23
python -m screener status
```

## 读路径规则

- 快照 `DONE + FULL + 版本一致 + rows 存在`：零写入，直接返回。
- 快照 `PARTIAL`：直接返回 `quality=PARTIAL`，不自动重算。
- 未命中且 `auto_build=true`：claim `BuildState`，内联构建，写回 `DONE/PARTIAL`。
- 未命中且 `auto_build=false`：抛 `ScreenerNotBuilt`。
- 显式 `rebuild_screener` / CLI：统一走 `BuildOrchestrator` + `InProcessJobDispatcher`。
- `as_of` 必须是交易日且不晚于最近完整交易日，否则抛 `InvalidScreenSpec`
  （避免构建出「当天还没收盘」的 PARTIAL 快照）。
- 全市场 `universe_key` 带**代码清单指纹**：新增上市/退市会改变 build_id，自动重建。

## 配置项（均已在代码中生效）

| 配置 | 作用 |
|---|---|
| `backfill.source_priority` | 日/周/月线回源顺序（透传给 loader，支持 baostock/akshare/tushare） |
| `backfill.intraday_source_priority` | 分钟线回源顺序（tushare 无分钟线，建议 baostock→akshare） |
| `backfill.max_lookback_bars` | 回看窗口上限；超出时截断并告警 |
| `backfill.max_sync_codes` | `get_screener_metrics` 内联同步的最大股票数，超出请用 `rebuild_screener` |
| `backfill.daily_chunk_years` / `minute_chunk_days` | 回源区间分块大小 |
| `query.default_limit` / `query.max_limit` | 未指定 limit 时的默认值与上限 |

停牌/退市/NoData 的日期通过 `skip_dates` 传给回源层：**不算缺口、不重复拉取**。
`market_suspension` 里 NoData 记录带 `retry_at`，到期后会自动重试。

基准指数默认不在读路径同步拉取（避免指数源慢拖垮筛股）。
需要 Beta 时，先运行 `python -m screener benchmark --start ... --end ...`，
或把 `config.json -> screener.backfill.fetch_benchmark` 设为 `true`。

## 依赖规则

- `domain` 不 import `data_interface`、`psycopg2`、`requests`，也不依赖外部回测框架。
- `application` 不 import `screener.infrastructure`，只依赖 `ports`。
- `infrastructure` 可以 import `data_interface`。
- `bootstrap` 是唯一装配具体实现的地方。
