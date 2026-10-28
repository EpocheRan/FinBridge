# FinBridge 架构 v2.1

> 状态：草案（按评审意见二次修订）
> 日期：2026-10-28
> 本文件是模块级架构的权威文档；因子内部细节见 `factor_library_v1.md`。

---

## 0. 修订摘要（v2.0 → v2.1）

| # | 评审问题 | v2.1 决策 |
|---|---|---|
| 1 | raw 表 vs factor 表没清单，财务/分档因子可能双份实现 | 新增 §2.1 完整清单；规则：派生指标只存在于 factor 表，raw 表不存“算出来的因子” |
| 2 | 长表投影缓存没定义（最大性能风险） | 新增 §4.5 三层存储：canonical 长表 + `factor_value_latest`（最新日）+ 可选 `factor_panel_cache`（Parquet，按 factor_id+version 分区）；所有权归 factor_engine |
| 3 | screener 也碰 DB，与规则矛盾 | 修正：DB 只由 `data_interface`（raw 表）与 `factor_engine`（factor 表 + 缓存）访问；screener **0 直连 DB**，一律走 port |
| 4 | 事件/进度协议所有权未定，acquisition↔factor_engine 会循环 | 事件与进度协议下沉到 `data_interface/common`；acquisition 单向调用 factor_engine，factor_engine 不 import acquisition |
| 5 | 绝对误差 1e-9 对市值/成交额必然误报 | 引入分因子容差：默认相对 1e-9，近零用绝对下限 1e-12；audit 与增量抽检共用 |
| 6 | backfill_*.py 与 acquisition 并行命令 | 统一入口为 `acquisition update/run`；`backfill_*.py` 迁移为 `acquisition/datasets/*.py`，旧脚本删除或薄包装 |

---

## 1. 模块划分（最终版）

| 模块 | 职责 | 允许依赖 |
|---|---|---|
| `factors/` | 纯因子库：分类、注册表、数学、定义、面板、DAG | 仅 pandas / numpy |
| `factor_engine/` | 计算引擎 + 因子存储 + 缓存 + 对账 + 迁移校验 | `factors` + `data_interface` |
| `screener/` | 纯筛选引擎：universe、白名单、筛选/排序、停牌缓存 | `factor_engine`(读) + `data_interface`(读服务) + `factors`(spec) |
| `acquisition/` | 整合数据获取：编排、进度、Scope、入库事件 | `data_interface` + `factor_engine`(单向调用) + `factors`(spec) |
| `backtest/` | 离线回测/研究 | `factors` + `data_interface` + `factor_engine`(读) |
| `data_interface/` | 底层数据访问（raw 表唯一写者） | common/config/questdb + 第三方源 |
| `position/` | 组合/持仓/交易 | `data_interface` + `factors`(technical) |

> `scheduled_tasks/` 由 `acquisition/` 取代；`screener/` 的计算职责迁到 `factor_engine/`。

---

## 2. 依赖方向与 DB 所有权

```text
                    ┌──────────────┐
                    │   factors    │  （纯定义 + 数学）
                    └──────┬───────┘
         ┌─────────────────┼──────────────────┐
         ▼                 ▼
  ┌──────────────┐  ┌──────────────┐
  │ factor_engine │  │   backtest   │
  │ (计算+存储)   │  │  (回测/研究) │
  └───┬──────────┘  └──┬───────────┘
      │                │
  ┌───▼────────────────▼────────────┐
  │          data_interface         │  raw 表唯一写者；DB 访问者之一
  └───┬─────────────────────────────┘
      │
  ┌───▼──────────────┐      ┌──────────────┐
  │   acquisition    │─────►│ factor_engine │（单向：发事件/触发重试）
  │  (整合数据获取)  │      └──────────────┘
  └───┬──────────────┘
      │
  ┌───▼──────────────┐
  │     screener     │  （0 直连 DB，只读 port）
  └──────────────────┘
```

**硬性规则**：

1. `factors` 不得 import screener/backtest/data_interface/factor_engine/acquisition。
2. **DB 访问只有两方**：`data_interface`（raw 表）与 `factor_engine`（factor 表 + 缓存）。screener/acquisition/backtest 一律 0 直连 QuestDB。
3. `screener` 不做任何因子计算；只通过 port 调 `factor_engine.ensure()`，然后读值。
4. `acquisition` 只编排拉数；算因子委托 `factor_engine`；且 `factor_engine` 永不 import `acquisition`。
5. 因子表（含缓存）只有 `factor_engine` 写；raw 表只有 `data_interface` 写。

---

## 2.1 raw 表 vs factor 表清单

### raw 表（`data_interface` 写，只有事实/原始字段，无派生因子）

| 域 | 表 |
|---|---|
| 行情 | `ohlcv_1m~60m`、`ohlcv_1d/1w/1mo`、`stock_basic_info`、`fund_basic_info`、`trading_days` |
| 财务 | `fin_report_meta`、`fin_income_statement`、`fin_balance_sheet`、`fin_cash_flow`、`fin_indicator`、`fin_estimate`、`fin_valuation_daily` |
| 分档 | `moneyflow_daily`、`moneyflow_source` |
| 概念 | `concept_board_dim`、`concept_board_daily`、`concept_member_snapshot`、`concept_classification`、`concept_board_alias` |
| 组合 | `portfolios`、`positions`、`trade_records` |
| 状态 | `acquisition_run`、`scheduled_task_state`（游标） |

### factor 表（`factor_engine` 写，所有“算出来的值”）

| 表 | 说明 |
|---|---|
| `factor_registry` | 因子目录（元数据 + 标签 + 版本 + DAG） |
| `factor_alias` | 旧 id → canonical 映射 |
| `factor_value_daily` | 时点型因子值（长表，版本化） |
| `factor_value_announce` | 事件/财务型因子值（available_at） |
| `factor_value_latest` | 最新日缓存（筛选快查） |
| `factor_panel_cache` | 可选 Parquet 面板缓存（回测加速） |
| `factor_build_state` | 构建/钩子状态、重试 |
| `factor_audit_state` | 对账与增量抽检结果 |

> `factor_value_intraday`（freq=5m 分时因子）**延后**：分钟线基线暂保留专用表 `intraday_baseline`，待有稳定分钟线数据源再引入。

### 防双份实现规则

- `main_net_ratio`、`ep_ttm`、`roe`、`beta` 这类**派生值只存在于 factor 表**；`moneyflow_daily` 只存四档原始额量，`fin_indicator` 只当作 raw 输入。
- 同一口径若 vendor 已给（如 `fin_indicator.roe`），factor_engine 可引用 raw 字段，但**因子值仍写 factor 表**，不把 raw 表当因子表用。

---

## 3. screener：纯筛选引擎（0 直连 DB）

### 保留

- `ScreenSpec/ScreenResult`、字段白名单、`query_builder`、universe 指纹、`as_of` 校验。
- 停牌/退市/NoData 负缓存（读侧，经 `data_interface` 服务访问）。
- 5 个 MCP 工具接口不变。

### 数据访问方式

| 需要 | 走谁 |
|---|---|
| 因子值 | `factor_engine.read(...)`（命中 `factor_value_latest` / 长表 / 缓存） |
| universe / 基础信息 | `data_interface.market` 读服务 |
| 停牌/NoData | `data_interface` 读服务 |
| 就绪保证 | `factor_engine.ensure(universe, as_of)` |

screener 不持有 QuestDB 连接、不 import psycopg2/questdb_client。

---

## 4. factor_engine：计算引擎 + 因子存储 + 缓存

### 4.1 能力

每日计算、落库时计算（钩子）、按需 ensure、回补、存储、版本、增量、对账、迁移校验。

### 4.2 目录

```text
factor_engine/
├── bootstrap.py
├── store.py           # 仓储（唯一写 factor 表）
├── read.py            # 读 API（screener/backtest 入口）
├── service.py         # 计算 + 写库 + ensure + backfill
├── hooks.py           # on_data_written
├── versions.py        # source_version
├── audit.py           # 对账 + 增量抽检
├── cache.py           # factor_value_latest / panel_cache
├── definitions/
└── cli.py
```

### 4.3 分因子容差（修正绝对 1e-9）

```python
@dataclass(frozen=True)
class Tolerance:
    mode: Literal["relative", "absolute"] = "relative"
    value: float = 1e-9
    floor: float = 1e-12        # 近零时的绝对下限

def close_enough(a, b, tol: Tolerance) -> bool:
    return abs(a - b) <= max(tol.floor, tol.value * max(abs(a), abs(b), 1.0))
```

- 默认**相对 1e-9**；市值/成交额/`close×avg_volume` 这类大值因子不再误报。
- `factor_projection_audit` 与增量抽检都用 `FactorSpec.tolerance`。
- 特殊因子（金额/股数）可显式配 `absolute`。

### 4.4 增量

`shift / window / recursive / event`（见 factor_library_v1.md）。增量抽检用相对容差，漂移即全量重建。

### 4.5 投影缓存（本次补定义，所有权 factor_engine）

三层：

| 层 | 形态 | 用途 | 构建/失效 |
|---|---|---|---|
| canonical | `factor_value_daily/announce/intraday` | 事实来源 | 每次计算写；版本化不可变 |
| latest | `factor_value_latest`（QuestDB，`(code, factor_id)`） | 筛股“最新日”快查 | 每日增量 upsert；版本切换整表重建 |
| panel | `factor_panel_cache`（Parquet，按 `factor_id/version/yyyy` 分区） | 回测长区间读加速 | 按需物化；`(factor_id, version, source_version)` 命中，否则重建 |

- screener 请求 → factor_engine.read → 优先 latest；未命中走长表 + 按需计算（预算内）。
- backtest 请求 → 优先 panel_cache；缺失回退长表 pivot（内存）。
- 缓存键含 `version + source_version`；上游版本变化自动失效重算。
- **缓存只 factor_engine 建/清**；screener/backtest 不写缓存。

---

## 5. factors：纯因子库

见 `factor_library_v1.md`。本次对齐：

- 移除 `data_interface/factor/`，存储/服务上移 `factor_engine/`。
- `FactorSpec` 增加 `tolerance`。
- `factors/pipeline.py` 只保留纯编排（端口注入）。

---

## 6. acquisition：整合数据获取

### 6.1 目标（不变）

一键补齐到最近完整交易日；行情/基础信息/基准/财务/估值/分档/概念/**预计算因子**；进度条；单代码/板块 × 日期段。

### 6.2 Scope（泛化）

```python
class Scope:
    codes: list[str] | None     # 股票/基金
    boards: list[str] | None
    start: date
    end: date
    datasets: tuple[str, ...]
```

### 6.3 数据集顺序

`trading_calendar → stock/fund_basic → ohlcv_1d+adj_factor+benchmark → financial → valuation → moneyflow → concept → factors`。

### 6.4 统一入口（修正 backfill 并行命令）

```bash
python -m acquisition run                              # 全市场补齐到最后交易日
python -m acquisition run --datasets ohlcv_1d,moneyflow,factors
python -m acquisition update --code 600519 --start 2024-01-01 --end 2026-10-27
python -m acquisition update --board 300008 --start 2024-01-01
```

- `backfill_moneyflow.py` / `backfill_concept.py` / `backfill_factors.py` 迁移为 `acquisition/datasets/{moneyflow,concept,factors}.py`，逻辑复用；旧脚本删除（不保留并行命令）。
- 每个数据集都是一个 `DatasetSyncer`：`plan/run/resume`，自带游标。

### 6.5 事件与进度协议所有权（修正循环）

| 协议 | 定义位置 | 实现/所有者 |
|---|---|---|
| `DataWritten(dataset, dates, codes)` | `data_interface/common/events.py` | acquisition 发，factor_engine 订阅处理 |
| `ProgressReporter` / `ProgressEvent` | `data_interface/common/progress.py` | acquisition 实现（tqdm + 状态表）；传给各 Syncer 与 factor_engine |

- `factor_engine.hooks.handle(DataWritten)` 写 `factor_build_state`；重试归 `factor_engine.retry_pending()`。
- acquisition 在数据集完成后：发 `DataWritten` → 调 `factor_engine.retry_pending(max)`，进度经同一个 `ProgressReporter` 汇总。
- **方向单一**：`factor_engine` 不 import `acquisition`；共享类型放 `data_interface/common`，两边都只依赖它，无环。

### 6.6 进度输出

控制台 `tqdm`（每数据集一条，含当前日期/代码 + ETA）；MCP `get_acquisition_progress()`；状态表 `acquisition_run`。

---

## 7. 一次性迁移（无并行）

### 7.1 原则

迁移窗口只写新表，旧表冻结；完成后 screener/backtest 只读新表；不双写、不回读旧表做增量。

### 7.2 映射

| 旧表 | 新表 |
|---|---|
| `screener_daily`（56 列） | `factor_value_daily`（每列一个 factor_id） |
| `screener_build_state` | `factor_build_state` |
| `market_suspension` | 数据层保留（data_interface 读） |
| `intraday_baseline` | **保留专用表**，不迁移（分钟线稳定源后再引入 `factor_value_intraday`） |

### 7.3 步骤

```text
M0 建新表
M1 写 registry + alias
M2 长表写入
M3 对账：行数一致 + 抽 20 日 × 100 只，分因子容差（默认相对 1e-9）
M4 切读（screener/backtest 只读新表；acquisition 只写新表）
M5 直接 DROP 旧表（不归档观察）：`screener_daily` / `screener_build_state`（`intraday_baseline` 保留专用表）
M6 重建 latest / panel 缓存
```

---

## 8. 功能更新清单

| 模块 | 更新 |
|---|---|
| `screener/` | 删除计算职责；0 直连 DB；读 factor_engine.read + data_interface 读服务 |
| `factor_engine/` | 新建；store/read/service/hooks/versions/audit/cache + CLI |
| `factors/` | indicator 数学并入；registry/alias/DAG/update_mode/tolerance |
| `acquisition/` | 新建；吸收 scheduled_tasks + backfill_*.py；进度/Scope/事件 |
| `data_interface/` | raw 表唯一写者；新增 common/events.py + common/progress.py |
| `backtest/` | `factor_source=stored` 走 factor_engine.read |
| `indicator/` | 已删除，并入 `factors.technical` |
| `MCPserver.py` | 注册 acquisition / factor_engine 工具 |
| `scripts/` | 新增 migrate_factors.py；backfill_*.py 迁入 acquisition 后删除 |

---

## 9. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0 | 新表 + registry/alias + `test_module_dependencies` + 事件/进度协议 | 依赖方向通过；协议类型可用 |
| M1 | factors 纯库化 + indicator 合并 + screener 计算抽离（0 DB） | 310 测试全绿；screener 无计算/无 DB import |
| M2 | factor_engine（compute/store/read/hooks/cache/audit/CLI） | 核心因子可算、可回补、版本化；相对容差对账通过 |
| M3 | 迁移 + 对账 + 切读 + 归档 | 新旧一致；无并行；缓存重建 |
| M4 | acquisition（编排/进度/Scope/事件，统一入口） | 一键补齐；单代码日期段可用；backfill 命令合并 |
| M5 | backtest stored + MCP + 文档 | 同策略 compute/stored 一致 |

---

## 10. 已决

- 分钟线：`intraday_baseline` **保留专用表**，暂不并入 `factor_value_intraday`；待有稳定分钟线数据源再调整。
- 旧表迁移后**直接 DROP，不归档观察**。
- MCP 启动**不自动** `acquisition.run`，默认手动；`acquisition.auto_run_on_start=true` 可选。
- `scheduled_tasks` 已删除，入口统一为 `acquisition`；同步实现位于 `acquisition/datasets/`。
- `factor_panel_cache` 延后：**P2 先做 latest**，回测出现瓶颈再加。

---

## 11. 实施状态（2026-10-28）

| 里程碑 | 状态 | 提交 |
|---|---|---|
| M0 新表/registry/alias/协议 | ✅ | `f6c9ce1` |
| M1 factors 纯库化 + indicator 合并 | ✅ | `295d2df` |
| M2 factor_engine 计算/存储/latest/钩子 | ✅ | `5dd4d70` |
| M3 screener_daily → factor_daily_wide 迁移 | ✅（3,522,492 行） | `5a89617` |
| M4 acquisition 编排/Scope/进度/MCP | ✅ | `bb8baf5` / `754cb22` |
| M5 stored 回测 + MCP + 文档 | ✅ | `cf5153b` |
| screener 56 列计算迁入 factor_engine.wide | ✅ | `4d993e2` |
| factor_daily_wide 写路径归 factor_engine | ✅ | `58a030d` |
| 23 个 legacy 因子补 compute | ✅ 复用 `factor_engine.wide`，5 内置 + 23 legacy = 28 | 最新 |
| `acquisition run` 默认包含 `screener` 宽表 | ✅ 同时保留 `--datasets` / 单代码 / 单板块 | 最新 |
| scheduled_tasks 完全退役 | ✅ 已删除；同步服务在 `acquisition/datasets/service.py` + `daily/financial/benchmark/screener/moneyflow/concept.py` | 最新 |
| build_orchestrator 迁入 acquisition | ✅ `acquisition/build_orchestrator.py` | 最新 |
| 文档统一到 docs / 删除 skills | ✅ | 最新 |
| 因子 materialization 标记（stored/inline/auto）| ✅ 回测逐因子混用 stored+inline | 最新 |

现状：`screener` 只保留字段映射、查询与 API；构建编排在 `acquisition`，因子计算在 `factor_engine`。

---

## 12. 后续清理

- 将 `acquisition/build_orchestrator.py` 的写调度进一步改为 `factor_engine.ensure_wide()`；
- 补迁移/回滚 runbook 与模块依赖图到 `docs/`。

