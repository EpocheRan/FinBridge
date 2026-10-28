# 因子库架构设计 v1.1

> 状态：草案（按评审意见修订）
> 日期：2026-10-28
> 目标：统一 `factors` / `indicator`，建立带分类、时点语义、版本血缘的因子库，支持入库、预计算、回测、筛股、MCP 多阶段调用。
>
> **模块归属以 [architecture_v2.md](architecture_v2.md) 为准**：本文件里的 `factor_engine/` 已上移为独立模块 `factor_engine/`，`screener` 只做筛选、计算全在 `factor_engine`，旧数据一次性迁移到新表。

---

## 0. 修订摘要（v1.0 → v1.1）

| # | 评审问题 | v1.1 决策 |
|---|---|---|
| 1 | 财务因子 `ts` 是报告期还是可用日未定义 | 拆成 **daily 时点表 + announce 事件表**；财务因子落 `factor_value_announce`，`ts = available_at`（公告日 + 披露滞后），另存 `report_period` / `announce_date` |
| 2 | 长表缺 `source_version`，复权重算破坏可复现 | 两张值表都加 `source_version`；价格因子记录复权因子快照版本，财务记录报表版本，分档记录源口径版本 |
| 3 | DEDUP 键不含版本，升级覆盖历史 | DEDUP 键加入 `version` + `source_version`；旧版本保留，查询默认取 registry 当前版本，可按版本回放 |
| 4 | 增量只算末尾一天，递归/长窗因子会漂移 | `FactorSpec.update_mode`：`shift / window / recursive / event`；recursive 必须从锚点重算，window 带足 lookback；增量结果与全量抽检按 `FactorSpec.tolerance`（默认相对 1e-9） |
| 5 | dependencies 命名空间与拓扑未定 | 依赖统一为 `raw:` / `factor:` / `ref:`；注册时建 DAG + 环检测；用拓扑层级替换 base/derived 两轮；行业用 `ref:industry`，不伪装成因子值 |
| 6 | `screener_daily` 投影一致性只留一句“需要校验” | 硬性 `factor_projection_audit`：长表投影 vs 宽表现算，抽样 + 全量，按分因子容差（默认相对 1e-9），定时执行并阻断发布 |
| 7 | `F.stored` 两代语义冲突 | 保留 `F.stored(column)` 读 legacy `screener_daily`；新增 `F.factor(factor_id, window=...)` 读长表；两套入口显式区分 |
| 8 | 新旧 id 并存、registry 重复注册 | 规范 id = `category.name`（窗口只做标签）；`factor_alias` 表维护 `mom20 → momentum.ret@20`；注册时拒绝 canonical/alias 冲突 |
| 9 | hooks 失败只记日志，静默不一致 | hooks 写 `factor_build_state`（FAILED/RETRY）+ 退避重试 + 健康度 `data_ts - factor_ts` 告警 |

**工期修正**：P5（财务时点语义）由 3–4 人日调整为 **5–7 人日**；总工期约 **25–30 人日**。

---

## 1. 目标与原则

### 目标

- 因子目录：分类、来源、窗口/频率、公式、依赖、版本全部可查询。
- 预计算：核心因子每日/每周增量落库，幂等、可回补、可审计。
- 多阶段调用：入库钩子、定时任务、回测、筛股、MCP、研究共用同一份定义。
- 口径唯一：ATR/RSI/动量/波动率等只保留一份实现。
- 可复现：因子版本 + 数据源版本 + 时点语义进入 `run_id`。

### 原则

1. `factors/` 纯 pandas/numpy，不 import DB、backtest、screener、indicator。
2. 因子值必须能回答三个问题：**什么时候可用**、**用哪版数据算的**、**公式是哪版**。
3. 增量计算不得改变历史值；改变历史值必须走版本升级。
4. 同一因子只有一种 canonical id；旧 id 只能做 alias。

---

## 2. 现状诊断

| 问题 | 位置 | 影响 |
|---|---|---|
| 元数据不全 | `factors/library.py` | 无分类/来源/频率，无法建目录 |
| 依赖方向反了 | `factors/ops.py` import indicator/screener | 纯因子库不纯 |
| ATR 三份 | `indicator/ATR.py`、`screener/domain/factors.py`、`factors/ops.py` | 口径漂移 |
| 只有 compute | `factors/panel.py` 对 stored raise；回测 stored 报错 | 预计算无法复用 |
| screener 宽表唯一落库 | `screener_daily` 56 列 | 新因子只能改宽表 |
| 无时点语义 | 财务因子未定义可用日 | 未来函数风险 |
| 无预计算编排 | 依赖筛股构建 | 入库后不自动刷新 |

---

## 3. 总体架构

```text
              factor_registry（元数据 + 3 类标签 + 版本 + DAG）
                                   │
                                   ▼
   ┌──────────────┐      ┌──────────────────────┐      ┌────────────────────┐
   │ 数据源        │─────►│ FactorDataBundle     │─────►│ FactorCompute      │
   │ ohlcv_1d     │      │ price / reference    │      │ ops + definitions  │
   │ fin_*        │      │ financial(as-of)     │      │ DAG 拓扑执行        │
   │ moneyflow    │      │ moneyflow / concept  │      └─────────┬──────────┘
   │ concept      │      │ benchmark            │                │
   │ analyst      │      └──────────────────────┘                │
   └──────────────┘                                              ▼
                        ┌─────────────────────────────────────────────────┐
                        │ factor_value_daily（时点型）                      │
                        │ factor_value_announce（事件/财务型）              │
                        └───────────────┬─────────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
     ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
     │ backtest panel  │       │ screener 宽表投影│       │ MCP / research  │
     │ compute/stored  │       │ + 对账任务        │       │ 目录/查询/导出   │
     └─────────────────┘       └─────────────────┘       └─────────────────┘
```

---

## 4. 分类体系

| 类别 | 子维度 | 典型因子 | 数据就绪 |
|---|---|---|---|
| Value 价值 | 盈利估值、资产估值、收入估值、现金/分红 | EP、BP、SP、股息率 | ✅ 财务 |
| Quality 质量 | 盈利能力、盈利质量、稳定性、财务安全 | ROE、OCF/NI、毛利率波动 | ✅ 财务 |
| Growth 成长 | 收入、利润、资产/现金流增速 | 营收 YoY、净利 YoY、3 年 CAGR | ✅ 财务 |
| Momentum 动量 | 收益动量、相对强度、加速 | 20/60/120 日收益、超额 | ✅ 日线 |
| Reversal 反转 | 短期 1~20 日、长期 3~5 年 | 5 日反转、20 日反转 | ✅ 日线 |
| Size 规模 | 总市值、流通市值、自由流通、对数 | log(total_mv) | ✅ 基础信息 |
| Liquidity 流动性 | 换手、成交额、冲击成本、深度 | 换手率、Amihud | ✅ 日线；深度需 L2 ❌ |
| Risk 风险 | 总波动、特质波动、Beta、下行风险 | 20/60 日波动、Beta、最大回撤 | ✅ 日线 |
| Sentiment 情绪/资金流 | 资金净流入、融资融券、北向 | 主力净流入、净流入率 | ✅ 分档；融资/北向待接 ⚠️ |
| Analyst 分析师预期 | 盈利预测、评级、目标价 | EPS 预测、评级上调 | ⚠️ 历史受限 |
| Leverage 杠杆 | 资产负债、财务杠杆、经营杠杆 | 资产负债率、D/E | ✅ 财务 |
| Technical 技术形态 | 均线乖离、RSI、突破强度 | MA 乖离、RSI、突破 N 日高点 | ✅ 日线 |

> ZJFZ 按复算结果归 `technical.reversal`（追击反转，LOW + KDJ），不归 sentiment；真正的资金分档归 `sentiment.moneyflow`。

---

## 5. 因子元数据

### 5.1 三个必填标签

| 标签 | 字段 | 示例 |
|---|---|---|
| 类型 | `category` / `subcategory` | `momentum` / `return` |
| 数据来源 | `source.dataset` / `source.vendor` / `source.fields` | `ohlcv_1d` / `questdb` / `close` |
| 窗口/频率 | `window` / `freq` / `lag` | `20` / `1d` / `0` |

### 5.2 `FactorSpec`

```python
class ValueKind(str, Enum):
    DAILY = "daily"          # 交易日时点，收盘后可用
    EVENT = "event"          # 公告/事件驱动，available_at 可用

class UpdateMode(str, Enum):
    SHIFT = "shift"          # 只依赖 t-N 对齐，可安全增量
    WINDOW = "window"        # 窗口型，需 t-(window+warmup) 历史
    RECURSIVE = "recursive"  # EMA/RMA/RSI 等递推，必须从锚点重算
    EVENT = "event"          # 公告驱动，按 report_period 更新

@dataclass(frozen=True)
class Dependency:
    kind: Literal["raw", "factor", "ref"]
    name: str                # raw:close / factor:momentum.ret / ref:industry

@dataclass(frozen=True)
class FactorSpec:
    factor_id: str                  # momentum.ret（窗口不进 id）
    name: str
    category: Category
    subcategory: str
    source: SourceSpec
    window: int
    freq: Freq                      # 1d / 5m / event
    value_kind: ValueKind
    update_mode: UpdateMode
    unit: Unit
    direction: Direction
    price_basis: PriceBasis
    version: str
    source_version_policy: str      # 如何生成 source_version
    dependencies: tuple[Dependency, ...]
    warmup: int
    lag: int = 0
    recompute_anchor: date | None = None
    formula: str = ""
    aliases: tuple[str, ...] = ()
    tolerance: Tolerance = Tolerance()   # 对账/增量抽检容差，默认相对 1e-9
    enabled: bool = True
```

```python
@dataclass(frozen=True)
class Tolerance:
    mode: Literal["relative", "absolute"] = "relative"
    value: float = 1e-9
    floor: float = 1e-12        # 近零时的绝对下限

def close_enough(a, b, tol: Tolerance) -> bool:
    return abs(a - b) <= max(tol.floor, tol.value * max(abs(a), abs(b), 1.0))
```

`factor_id` 规范：`{category}.{name}`，窗口只做标签。需要固定窗口实例时用 `window=20`，别名可写 `momentum.ret@20`。

### 5.3 注册

```python
@factor(
    "momentum.ret",
    category="momentum", subcategory="return",
    source=SourceSpec(dataset="ohlcv_1d", vendor="questdb", fields=("close",)),
    window=20, freq="1d", value_kind="daily", update_mode="shift",
    unit="ratio", direction="positive", price_basis="qfq", version="v1",
    aliases=("mom20",),
)
def ret_20d(close: pd.Series) -> pd.Series:
    return close.pct_change(20)
```

- canonical 冲突、alias 冲突、DAG 环检测都在注册时失败。
- `resolve_id("mom20")` → `("momentum.ret", window=20)`。
- `F.mom(20)` 内部解析到 canonical。

---

## 6. 时间语义（v1.1 新增，最关键）

### 6.1 两类因子，两张表

| 类型 | 表 | `ts` 含义 | 例子 |
|---|---|---|---|
| 时点型 `daily` | `factor_value_daily` | 交易日，收盘后可用，T+1 开盘可交易 | 动量、ATR、资金流、板块 |
| 事件型 `event` | `factor_value_announce` | **`available_at`**，即第一次可用的日期，不是报告期 | EP、ROE、营收增速、评级 |

财务因子绝不能把 `ts` 写成 `report_period`（例如 2024-12-31），否则回测会在 2024-12-31 就读到 2025-04 才公告的数据。

### 6.2 `factor_value_announce` 字段

```sql
CREATE TABLE IF NOT EXISTS "factor_value_announce" (
    ts TIMESTAMP,              -- available_at：公告日 + disclosure_lag_days
    code SYMBOL,
    factor_id SYMBOL,
    report_period TIMESTAMP,   -- 报告期，例如 2024-12-31
    announce_date TIMESTAMP,   -- 实际公告日
    lag_days INT,              -- 披露滞后天数
    value DOUBLE,
    quality SYMBOL,
    source SYMBOL,
    source_version SYMBOL,
    version SYMBOL,
    computed_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(ts, code, factor_id, report_period, version, source_version)
```

规则：

- `available_at = announce_date + lag_days`；若公告在收盘后，`available_at` 取下一交易日 00:00。
- 同一 `report_period` 可能因更正/重述出现多条，用 `version` / `source_version` 区分。
- PIT 查询：`ts <= as_of` 中取 `max(ts)`，同一 `report_period` 取最新 `announce_date`。

### 6.3 财务因子回测读取

```text
factor_as_of(code, factor_id, t):
    取 factor_value_announce 中 ts <= t 的最近一条
    → 若该报告期有重述，取 ts 最新的那条
```

可选优化：对核心财务因子物化一份 `factor_value_daily` 缓存（按 `available_at` 向后填充），但 canonical 仍是 announce 表。

### 6.4 时点测试

- 构造“公告日 2025-04-25、报告期 2024-12-31”的样本，断言 `as_of=2025-04-24` 查不到、`2025-04-25` 查得到。
- 重述场景：同一报告期两条公告，断言旧 run 用旧版本、新 run 用新版本。

---

## 7. 存储模型

### 7.1 `factor_registry`

```sql
CREATE TABLE IF NOT EXISTS "factor_registry" (
    updated_at TIMESTAMP,
    factor_id SYMBOL,
    name STRING,
    category SYMBOL,
    subcategory SYMBOL,
    source_dataset SYMBOL,
    source_vendor SYMBOL,
    source_fields STRING,
    freq SYMBOL,
    window INT,
    lag INT,
    value_kind SYMBOL,          -- daily / event
    update_mode SYMBOL,         -- shift / window / recursive / event
    unit SYMBOL,
    direction SYMBOL,
    price_basis SYMBOL,
    version SYMBOL,             -- 当前公式版本
    source_version_policy STRING,
    dependencies STRING,        -- raw:close;factor:...;ref:...
    warmup INT,
    formula STRING,
    enabled BOOLEAN
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, factor_id)
```

### 7.2 `factor_value_daily`

```sql
CREATE TABLE IF NOT EXISTS "factor_value_daily" (
    ts TIMESTAMP,
    code SYMBOL,
    factor_id SYMBOL,
    value DOUBLE,
    quality SYMBOL,
    source SYMBOL,
    source_version SYMBOL,      -- 上游数据版本（复权因子快照/源批次）
    version SYMBOL,             -- 公式版本
    computed_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, code, factor_id, version, source_version)
```

- **版本化不可变**：换公式或换上游数据版本写新行，旧行保留。
- 查询默认取 `factor_registry.version` 对应行；研究/回放可指定 `version` / `source_version`。
- 存储增长只发生在版本切换，不随日常更新增长。

### 7.3 `factor_build_state`

```sql
CREATE TABLE IF NOT EXISTS "factor_build_state" (
    updated_at TIMESTAMP,
    job_id SYMBOL,
    dataset SYMBOL,             -- ohlcv_1d / fin / moneyflow / manual
    factor_ids STRING,
    window_start TIMESTAMP,
    window_end TIMESTAMP,
    cursor TIMESTAMP,
    status SYMBOL,              -- PENDING/RUNNING/DONE/FAILED/RETRY
    attempts INT,
    retry_at TIMESTAMP,
    rows_written LONG,
    last_error STRING,
    payload STRING
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, job_id)
```

### 7.4 `factor_alias`

```sql
CREATE TABLE IF NOT EXISTS "factor_alias" (
    updated_at TIMESTAMP,
    alias SYMBOL,
    factor_id SYMBOL,
    window INT,
    params STRING,
    notes STRING
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, alias)
```

`mom20 → (momentum.ret, 20)`、`close_vs_ma20_pct → (technical.ma_deviation, 20)`、`atr14 → (technical.atr, 14)`。
注册时校验 alias 不冲突；`F.mom(20)`/`F.stored("mom20")` 都先 `resolve_id`。

---

## 8. 版本与可复现

### 8.1 `source_version` 生成规则

| 数据源 | source_version 示例 | 触发变化 |
|---|---|---|
| 日线/复权 | `adj:{max_ts}:{adj_factor_hash}` | 复权因子重算 |
| 财务 | `report:{report_id}:{update_flag}` | 更正/重述 |
| 分档 | `tushare.moneyflow.v1:{fetch_batch}` | 源口径/批次变化 |
| 概念 | `ths:{snapshot_date}` | 快照变化 |

由 `FactorSpec.source_version_policy` 声明，`factor_engine` 统一生成。

### 8.2 `run_id`

`run_id` 纳入：registry 版本哈希 + 每个 factor 的 `version` + `source_version` + `value_kind`。
同一策略、同一区间，换版本或换上游数据必须换 `run_id`；旧 `run_id` 可按旧版本行回放。

### 8.3 覆盖策略

- **不覆盖**：DEDUP 键含 `version` + `source_version`，旧行保留。
- 查询默认用 registry 当前版本；`loaded_rows` 记录实际版本。
- 清理策略：只清理超过 N 年且无 run 引用的旧版本（后续实现）。

---

## 9. 计算与增量

### 9.1 `update_mode`

| 模式 | 适用 | 增量策略 |
|---|---|---|
| `shift` | 收益率、滞后差分 | 只算新增日期 |
| `window` | MA、beta 252、52 周高低、corr、Amihud | 从 `t - (window + warmup + buffer)` 重算 |
| `recursive` | EMA/RMA、Wilder ATR、RSI、EWMA 波动 | 从 `recompute_anchor` 或上次全量点重算；禁止只算末尾 |
| `event` | 财务/分析师 | 按 `report_period` 新公告增量 |

### 9.2 增量校验

- 每次增量后抽 20 个 `(code, date)` 与全量重算比对，按 `FactorSpec.tolerance`（默认相对 1e-9，近零绝对下限 1e-12）。
- 超过阈值：标记 `factor_id` 为 `RECURSIVE_DRIFT`，强制全量重建并记录状态。
- 递归因子在 registry 里显式标记，回补脚本按 `update_mode` 选择起点。

### 9.3 依赖 DAG

- `Dependency(kind, name)`：`raw:close`、`factor:momentum.ret`、`ref:industry`。
- 注册时做环检测；执行按拓扑层级，不再硬分 base/derived 两轮。
- `ref:` 从 `FactorDataBundle.reference` 取（行业、市值标签），不写进因子值。
- 中性化：`neutralize(factor, by=("ref:industry", "factor:size.log_mv"))` 作为 DAG 的一个算子节点；行业保持标签语义，不建“行业因子”。

---

## 10. 预计算流水线

### 10.1 步骤

```text
resolve specs(category, freq, value_kind)
  → DAG 拓扑分层
  → 按 (freq, source_dataset) 分组加载 FactorDataBundle
  → 逐因子 compute（shift/window/recursive 各自起点）
  → 质量检查
  → 写 daily 或 announce 表（版本化）
  → 更新 build_state；投影 screener_daily；对账
```

### 10.2 入库钩子

| 数据写入 | 触发 |
|---|---|
| `ohlcv_1d` | momentum / reversal / size / liquidity / risk / technical |
| `moneyflow_daily` | sentiment.moneyflow |
| 财务同步 | value / quality / growth / leverage（event） |
| `concept_board_daily` | concept 类 |
| 分析师同步 | analyst（受限） |

**失败处理（v1.1 修正）**：

- hook 先写 `factor_build_state(status=PENDING)`，执行后 `DONE/FAILED`。
- 失败任务带 `attempts` / `retry_at`，调度器指数退避重试，超过上限转 `DEAD` 并告警。
- 健康度检查：`max(data_ts) - max(factor_ts)` 超过阈值（日频 1 个交易日、事件型 1 个公告周期）即告警。
- 进程崩溃时 `PENDING/RUNNING` 会被下次启动接管。

### 10.3 定时任务

- `dataset="factors"`：核心因子日更。
- `dataset="factors:event"`：财务/分析师事件型更新。
- `scripts/backfill_factors.py --start --end --category --mode`。

---

## 11. backtest 融合

### 11.1 入口区分

| API | 数据源 | 用途 |
|---|---|---|
| `F.stored(column)` | legacy `screener_daily` 列 | 兼容旧策略；文档标记 deprecated |
| `F.factor(factor_id, window=None)` | `factor_value_daily` / `announce` | 新因子库 |

- `factor_source=compute|stored|auto` 只作用于 `F.factor`；`F.stored` 始终走 screener_daily 映射，避免语义打架。
- `build_panel` 按 FactorDef 类型分派 loader。

### 11.2 panel

- `StoredFactorLoader`：按 `(factor_id, version, source_version)` 查询；事件型按 as-of 解析。
- `ComputeFactorLoader`：现算。
- `auto`：覆盖率 + 版本匹配才用 stored，否则 compute，并在 meta 里记录 fallback 原因。

### 11.3 `run_id`

纳入 registry 哈希、因子版本、source_version、value_kind；事件型因子额外纳入 `report_period` 解析规则版本。

---

## 12. screener 融合与对账

- 短期：`screener` 继续写 `screener_daily`，公式从 `factors.definitions` 取。
- 中期：因子服务写长表后投影核心列；`screener_daily` 是缓存，不是事实来源。
- **对账（硬性验收）**：
  - `factor_projection_audit` 抽样（20 交易日 × 100 只 × 映射因子）与全量（手动触发）比对长表投影 vs 宽表现算，按分因子容差（默认相对 1e-9，近零绝对下限 1e-12）。
  - 每日定时执行；失败写 `factor_build_state(FAILED)`，阻断 `screener_daily` 发布并告警。
  - 版本/`source_version` 不一致时直接判失败，不比较数值。

---

## 13. indicator 合并

| 现状 | 迁移后 |
|---|---|
| `indicator/ATR.py` | `factors/technical/atr.py` |
| `indicator/Engine.py` | `factors/engine.py`（`IndicatorEngine` 别名） |
| `screener/domain/factors.py` | `factors/ops/ts.py`，screener 侧 re-export |
| `position/modes/atr_mode.py` | import `factors.technical` |

- `indicator/` 只保留 shim + `DeprecationWarning`。
- 新增依赖方向测试：`factors` 不得 import screener/indicator/backtest/data_interface。
- `screener.domain.factors` 保留 re-export，内部实现只此一份。

---

## 14. 目录结构

```text
factors/
├── taxonomy.py
├── spec.py
├── registry.py          # 注册、alias、DAG、环检测
├── library.py           # F DSL（F.mom / F.factor / F.stored）
├── ops/
│   ├── ts.py
│   ├── technical.py
│   └── cross.py
├── definitions/
│   ├── price.py
│   ├── fundamental.py
│   ├── sentiment.py
│   ├── analyst.py
│   └── concept.py
├── compute.py
├── panel.py
├── stored.py            # legacy screener_daily 映射
└── pipeline.py          # 纯编排（端口注入）

factor_engine/
├── repository.py        # registry / daily / announce / state / alias
├── service.py           # 预计算、查询、钩子
├── versions.py         # source_version 生成
└── audit.py            # 投影对账

scripts/
└── backfill_factors.py
```

---

## 15. 分期与验收

| 阶段 | 交付 | 验收 | 人日 |
|---|---|---|---|
| P0 | taxonomy / spec / registry / alias / DAG | 现有因子有 3 类标签；alias 冲突/环检测测试 | 3 |
| P1 | indicator 合并 + screener 纯函数迁移 + shim | 310 测试全绿；无反向依赖 | 2 |
| P2 | registry / daily / announce / state / pipeline / CLI | 增量幂等；版本化不覆盖；时点测试通过 | 4 |
| P3 | backtest `F.factor` + stored/auto + run_id | 同一策略 compute/stored 结果一致 | 3 |
| P4 | 价格类因子 | 与旧 screener 列抽检（分因子容差） | 3 |
| P5 | 财务类因子 + 时点语义 | 公告日前后断言；重述可回放；无未来函数 | **5–7** |
| P6 | 资金流/分析师/概念 + hooks | 分档同步后自动更新；失败重试 | 3 |
| P7 | screener 投影 + 对账 + MCP | 每日对账（分因子容差）；MCP 可查目录/值 | 3 |

总计约 **26–28 人日**（P5 上调后）。

---

## 16. 风险与待决

1. **长表体量**：核心集 40–60 个；版本化保留旧行，需设定清理策略。
2. **财务重述**：同一报告期多版本，回测必须按 `available_at` + `version` 解析，不能取“最新”。
3. **分析师数据**：历史覆盖有限，先做当前快照，不承诺长历史。
4. **screener 迁移**：双写期对账必须每日跑，不能靠人工。
5. **递归因子**：任何“只算末尾”的实现都要在 code review 里拒绝。
6. **ZJFZ**：归 `technical.reversal`，不归 sentiment。
7. **行业中性化**：行业是 `ref:` 标签，不建数值因子；若需要行业哑变量，单独 `reference` 数据集。

---

## 17. 兼容策略

- `FactorDef` 保留，新增可选字段；`F.mom(20)` 不破。
- `F.stored(column)` 继续读 `screener_daily`，标 deprecated；新代码用 `F.factor`。
- `build_panel` 默认 `compute`，旧测试不变。
- `indicator` 保留 shim。
- `screener_daily` 列名不变；`factors/stored.py` 维护 canonical 映射。
- `factor_source=stored` 从“直接报错”改为“读长表”；`auto` 为默认。
