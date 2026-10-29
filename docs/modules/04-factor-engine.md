# 04 · factor_engine（因子物化与注册）

## 1. 架构与依赖

### 1.1 职责边界

`factor_engine` 把 `factors` 里的定义**算出来并落库**，同时维护因子注册表。它解决三个问题：

1. **谁可以被查**：`factor_registry`（ID、分类、方向、单位、版本、物化方式）；
2. **什么时候算**：`STORED`（必须预计算）/ `INLINE`（逐 bar 现算）/ `AUTO`（缺失回退）；
3. **算完写哪**：`factor_value_daily`（长表）+ `factor_value_latest`（最新一期）+ `factor_build_state`。

它**不**做评级（→ `research`）、不做选股（→ `screener`）、不做回测（→ `backtest`）。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `factors` | 因子定义、`build_panel`、`compute_series` |
| 上游 | `data_interface` | `PriceData` 读 `ohlcv_1d`；`FundamentalComputer` 读 `fin_*` |
| 下游 | `screener`、`backtest`、`research` | 读 `factor_value_daily` / 调 `service.read()` |
| 被调用 | `acquisition.syncers.factors`、`MCPserver`（`factor_engine.api`） | 触发物化 / 工具 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `service.py` | `FactorEngineService`：**主入口**（build / ensure / read / 注册同步） |
| `store.py` | `FactorStore`：所有表的 upsert / query |
| `data.py` | `PriceData`：按代码+区间取日线并复权 |
| `wide.py` | `FactorEngine`：一次算 56 列宽表（legacy/technical） |
| `wide_store.py` | 宽表 → 长表行 / 落库 |
| `definitions/__init__.py` | `register_all()`：把全部 spec 注册进 registry |
| `definitions/price.py` | `PRICE_FACTORS`（技术类内置因子）+ `BuiltinFactor` |
| `definitions/price_factors.py` | `PRICE_LIB_SPECS`：价量因子库 85 个 spec（INLINE） |
| `fundamental.py` | `FundamentalComputer` + `FUNDAMENTAL_SPECS`（15 个成长/质量） |
| `fin_ext.py` | `FIN_EXT_SPECS`（61 个财务因子）+ `compute_ext` + `RANK_EXT` |
| `valuation.py` | `ValuationComputer` + `VALUATION_SPECS`（13 个估值） |
| `audit.py` / `versions.py` / `hooks.py` | 对账、版本戳、生命周期钩子 |
| `api.py` / `cli.py` | MCP 工具 / `python -m factor_engine` |

### 1.4 数据流

```
FactorEngineService.build(ids, start, end, codes)
   ├─ _ensure_registry()            注册全部 spec 到 factor_registry
   ├─ 分流：legacy / fundamental / valuation / compute
   ├─ PriceData.load()              → 因子帧（含 hfq_close/adj）
   ├─ factors.build_panel()         → 面板
   ├─ FundamentalComputer.load_metrics().compute()  → 财务长表
   ├─ 组装 records（每条：ts/code/factor_id/value/quality/source_version）
   ├─ 每 5 万行 upsert factor_value_daily + factor_value_latest
   └─ 写 factor_build_state（cursor/status/rows_written）
```

## 2. 核心类与方法

### 2.1 类表

| 类 | 关键方法 | 说明 |
|---|---|---|
| `FactorEngineService` | `build(ids, start, end, codes, write_latest=True)` | 批量物化 |
| | `ensure(codes, as_of, ids)` | 只在缺失时补算 |
| | `read(ids, start, end, codes)` / `read_latest(ids, codes)` | 查长表 / 查最新 |
| | `list_factors()` / `default_precompute_ids()` / `sync_registry()` | 注册表维护 |
| `FactorStore` | `upsert_values(frame)` / `upsert_latest` / `query_values` / `latest_ts` | 表操作 |
| `FundamentalComputer` | `load_metrics(codes, start, end, trade_dates)` → 48 列面板 | 报表 PIT 对齐 |
| | `compute(metrics)` → 长表 | 76 个财务因子（含 61 个扩展） |
| `PriceData` | `load(codes, start, end)` | 日线 + 复权 |
| `FactorEngine`(wide) | `compute_daily(raw, benchmark, reference)` | 56 列宽表 |

### 2.2 用法

```python
from factor_engine.definitions import register_all
from factor_engine.service import FactorEngineService

register_all()
svc = FactorEngineService()

# 物化（财务因子 STORED，必须落库）
svc.build(["quality.roe_ttm", "value.ep"], "2024-01-01", "2026-10-28", codes=["600519"])

# 读
frame = svc.read(["quality.roe_ttm"], "2024-01-01", "2026-10-28", codes=["600519"])
latest = svc.read_latest(["quality.roe_ttm"], codes=["600519"])
```

```python
# 只补缺失
svc.ensure(codes=["600519"], as_of="2026-10-28", factor_ids=["quality.roe_ttm"])
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **`build` 会吃掉内存** | `records` 是 Python dict 列表；百万行会顶满 16G。已改成分批（每 5 万行）写，且**单批代码数建议 ≤ 8** |
| **`fundamental_ids` 必须含 `FIN_EXT_SPECS`** | 否则 61 个财务因子会被当成 INLINE 去算，直接报"未知算子" |
| **STORED 因子回测只读** | 回测里缺失会报错，不会自动现算（`AUTO` 才会） |
| **PIT 覆盖率低会静默降效** | 财务因子在 2024+ 才有足够样本；覆盖率 < 70% 会被评级否决 |
| **`factor_value_daily` 巨大** | 查询必须带 `ts` 范围或 `factor_id`，否则全表扫 |
| **写后读要重试** | WAL 异步，`upsert` 后立即 `read` 可能为空 |
| **`register_all()` 幂等** | 重复调用安全；但新增 spec 后必须调用才会进 registry |
| **方向是设计先验** | `FactorSpec.direction` 是经济假设，不是实测结论；实测校准见 `scripts/calibrate_directions.py` |
| **宽表与长表要一致** | `wide.py` 的 56 列如果和 `legacy_specs()` 对不上，`LEGACY_COLUMNS` 会 KeyError |
