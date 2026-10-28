# 概念板块 & 分档数据 架构设计 v1

> 状态：草案，待评审
> 日期：2026-10-28
> 范围：`moneyflow`（个股日频分档）+ `concept`（概念板块元数据/成分/日线）

---

## 0.1 实现状态（2026-10-28）

| 能力 | 落点 | 状态 |
|---|---|---|
| 分档 schema | `scripts/schemas/moneyflow_schema.py` | ✅ 已建表 |
| 分档源 | `data_interface/moneyflow/sources/tushare_source.py` | ✅ 2022-10-10~2026-10-27 已回补：**5,014,483 行 / 982 个交易日 / 每日 4825~5577 只 / 全部 quality=ok** |
| 分档同步/查询 | `data_interface/moneyflow/{service,repository}.py` | ✅ 游标/质量/源回退/空日 pending 重试已单测 |
| 板块列表 | `data_interface/concept/sources/ths_source.py` | ✅ 实测 375 个 |
| 板块日线 | `data_interface/concept/sources/{ths_source,ths_fast}.py` | ✅ 2022-10-10~2026-10-27 已回补：**337,520 行 / 375 个板块 / 无内部年度缺口**；2022-10-10 当天有数据的板块 284 个（其余为后续成立） |
| 板块维度 | `scripts/backfill_concept.py --dataset dim` | ⚠️ 无历史列表源，2022-10-10 与 2026-10-27 为**当前列表投影**（`source=akshare.ths_index.reconstructed`，各 375 行）；日线按板块实际成立时间从 2022-10-10 或更晚开始 |
| 成分快照 | `concept/sources/{em_source,ths_scrape_source}.py` | ✅ 东财被断时回退同花顺，实测 18 / 9 只（两个板块） |
| 概念分类 | `concept/sources/tushare_classification.py` | ⚠️ 接口限 1 次/小时，实跑命中限流；映射已单测，下次窗口可补 |
| MCP | `data_interface/api/{moneyflow,concept}_tools.py` | ✅ 7 个工具已注册并实调用通过 |
| 离线测试 | `tests/test_moneyflow_*.py`、`tests/test_concept_*.py` | ✅ 310 passed / 12 skipped（全量套件） |

---

## 0. 结论摘要

两条独立的数据管线，共用一套「数据源标注 + 同步游标 + 质量门禁」约定：

- **分档（moneyflow）**：新增 `moneyflow_daily`，一行一个 `(交易日, 股票, 源)`；Tushare `moneyflow` 为主源，按交易日一次拉全市场（实测 5577 只/日，2010 年起有数据）。
- **概念（concept）**：新增 `concept_board_dim` / `concept_board_daily` / `concept_member_snapshot` / `concept_board_alias`；同花顺（AkShare）提供板块列表与板块日线，**成分股是唯一没有稳定免费源的部分**，需要先决策（见 §11）。
- 所有表带 `source`、`source_version`、`fetched_at`；查询结果回传 `data_source`，回测 `run_id` 纳入源与版本。
- 不改动现有 `ohlcv_1d` / `screener_daily`；旧的三张 `concept_*` 表在迁移完成后下线。

预计工作量：分档 2–3 人日；概念元数据+日线 2 人日；成分 1 人日（有 API）~ 4 人日（网页抓取）；因子/回测接入 2–3 人日。

---

## 1. 范围与非目标

### 1.1 本期目标

1. 获取并存储个股日频分档数据（超大/大/中/小单 × 主动买/卖），每行可追溯到数据源与抓取时间。
2. 获取当前全部概念板块的：元数据（代码/名称/类型）、成分股、板块日线（OHLC + 成交量 + 成交额）。
3. 提供按日期/板块/股票的查询接口（MCP），并为后续因子与回测预留接入点。

### 1.2 非目标

- 不落地逐笔成交/逐笔委托原始数据（量级与成本另议）。
- 不做板块资金流（可在分档数据就绪后，用成分股聚合派生）。
- 不做自动交易、不做实时盘中推送。
- 不重建历史成分（源不提供 in/out 日期时，只能从接入日开始积累快照）。

---

## 2. 现状与差距

### 2.1 已有

| 资产 | 位置 | 状态 |
|---|---|---|
| `concept_boards` / `concept_boards_ohlc` / `concept_constituents` | `scripts/schemas/market_schema.py` | 可用但字段不足 |
| `ConceptBoardsStrategy` / `ConceptBoardOhlcStrategy` | `data_interface/market/loader.py` | 已接线，未进定时任务 |
| 板块列表/日线/成分抓取 | `data_interface/market/sources/akshare_source.py` | 列表+日线可用；成分走东财 |
| MCP `get_concept_boards` | `data_interface/api/market_tools.py` | 只有列表 |

### 2.2 差距

1. **没有 source 字段**：三张表都无法回答「这条数据从哪来」。
2. **源不一致**：板块列表/日线走同花顺，成分走东方财富；两边板块名不一致时成分直接为空。
3. **没有 PIT 语义**：成分只存「最近一次」，无法还原历史某日的成分。
4. **板块日线字段不全**：无 `source`、`volume` 是 DOUBLE、缺 `pct_chg`。
5. **没有同步编排**：`scheduled_tasks.DATASETS` 只有 daily/benchmark/financial/screener。
6. **分档数据完全没有**：无表、无源、无工具。

---

## 3. 数据源实测（2026-10-28，本机）

| 数据 | 源/接口 | 结果 | 关键信息 |
|---|---|---|---|
| 个股四档分档 | Tushare `moneyflow` | ✅ 有权限 | 单日全市场 5577 行；单票 2020 起 1650 行；金额万元、量手 |
| 北向总量 | Tushare `moneyflow_hsgt` | ✅ 有权限 | 只有总量，无个股分档 |
| 板块列表 | AkShare `stock_board_concept_name_ths` | ✅ 375 个 | `name` + 6 位 `code`（如 308614） |
| 板块日线 | AkShare `stock_board_concept_index_ths` | ✅ 可用 | 日期/OHLC/成交量(股)/成交额(元) |
| 板块成分 | AkShare `stock_board_concept_cons_em` | ❌ 本机连接被断 | 需换网络/代理再验证 |
| 板块成分 | 同花顺网页 `q.10jqka.com.cn/gn/detail/code/{code}/` | ⚠️ 可抓但脆弱 | 10 行/页，单板块可到 35 页；需 `hexin-v` |
| 板块成分 | Tushare `ths_member` | ❌ 积分不足 | 需 5000 分 |
| 板块列表/日线 | Tushare `ths_index` / `ths_daily` | ❌ 积分不足 | 需 5000 分 |
| 概念分类 | Tushare `concept` | ✅ 3000 条 | 只有分类树，无成分、无行情 |
| 概念资金流 | AkShare `stock_fund_flow_concept` | ✅ 287 条 | 板块级资金流，非个股分档 |

**结论**：分档与板块元数据/日线都有可用源；**成分股是本期唯一需要拍板的缺口**。

---

## 4. 总体架构

```text
                       ┌─────────────────────────────────────────────┐
                       │        MCP tools / CLI / 策略 / 回测         │
                       │  get_concept_* / get_moneyflow_* / factors  │
                       └──────────────────────┬──────────────────────┘
                                              │
              ┌───────────────────────────────┼───────────────────────────────┐
              ▼                               ▼                               ▼
     ┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
     │ concept.service │            │ moneyflow.svc   │            │  factors/panel  │
     │ 查询/同步编排    │            │ 查询/同步编排    │            │  因子计算        │
     └────────┬────────┘            └────────┬────────┘            └────────┬────────┘
              │                              │                              │
     ┌────────▼────────┐            ┌────────▼────────┐                    │
     │ repository      │            │ repository      │                    │
     │ (QuestDB)       │            │ (QuestDB)       │                    │
     └────────▲────────┘            └────────▲────────┘                    │
              │                              │                              │
     ┌────────┴────────┐            ┌────────┴────────┐                    │
     │ source adapters │            │ source adapters │                    │
     │ ths / em / tushare / manual   │ tushare / em / l2                  │
     └────────▲────────┘            └────────▲────────┘                    │
              │                              │                              │
       同花顺 / 东财 / Tushare         Tushare / 东财 / 券商 L2            │
              └──────────────────────────────┴──────────────────────────────┘
                                             │
                                      ┌──────▼──────┐
                                      │   QuestDB   │
                                      └─────────────┘
```

### 4.1 分层与目录

```text
interface        data_interface/api/concept_tools.py
                 data_interface/api/moneyflow_tools.py
                 scheduled_tasks/api.py（新增 dataset）
application      data_interface/concept/service.py
                 data_interface/moneyflow/service.py
domain           data_interface/concept/models.py（Board / Member / Bar）
                 data_interface/moneyflow/models.py（MoneyflowRow / SourceMeta）
infrastructure   data_interface/concept/repository.py
                 data_interface/concept/sources/{ths,em,tushare,manual}.py
                 data_interface/moneyflow/repository.py
                 data_interface/moneyflow/sources/{tushare,em}.py
shared           scripts/schemas/concept_schema.py
                 scripts/schemas/moneyflow_schema.py
                 data_interface/common/units.py（单位换算）
```

依赖规则：`sources` 不 import `repository`；`service` 只依赖 domain + ports；`api` 只做参数校验与状态码。

---

## 5. 数据模型

### 5.1 全局约定

- **时间**：`ts` = 交易日 00:00（Asia/Shanghai）；`fetched_at` = 实际抓取时间；`updated_at` = 行变更时间。
- **单位**：金额一律「元」，量一律「股」，比例用小数（0.0123 而非 1.23）。源数据是万元/手的，在 adapter 内换算，仓库层不再做单位转换。
- **幂等**：QuestDB `DEDUP UPSERT KEYS`，同一 `(ts, code, source)` 重复写入覆盖。
- **source 命名**：`{vendor}.{dataset}[.{variant}]`，例如 `tushare.moneyflow`、`akshare.ths_index`、`akshare.em_board`、`manual.csv`。
- **绝不混源**：同一 `(ts, code)` 允许多个 source 并存；查询时按优先级取一个，并回传实际使用的 source。

### 5.2 分档：`moneyflow_daily`

```sql
CREATE TABLE IF NOT EXISTS "moneyflow_daily" (
    ts TIMESTAMP,
    code SYMBOL,
    market SYMBOL,

    buy_sm_vol LONG,   buy_sm_amount DOUBLE,
    sell_sm_vol LONG,  sell_sm_amount DOUBLE,
    buy_md_vol LONG,   buy_md_amount DOUBLE,
    sell_md_vol LONG,  sell_md_amount DOUBLE,
    buy_lg_vol LONG,   buy_lg_amount DOUBLE,
    sell_lg_vol LONG,  sell_lg_amount DOUBLE,
    buy_elg_vol LONG,  buy_elg_amount DOUBLE,
    sell_elg_vol LONG, sell_elg_amount DOUBLE,

    net_mf_vol LONG,
    net_mf_amount DOUBLE,

    source SYMBOL,             -- tushare.moneyflow / akshare.em / ...
    source_version SYMBOL,     -- 口径版本，源方改定义时递增
    quality SYMBOL,            -- ok / partial / suspect
    quality_note STRING,
    fetched_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, code, market, source)
```

派生指标不落库，查询/因子层算：

```text
main_amount      = (buy_lg + buy_elg) - (sell_lg + sell_elg)
main_ratio       = main_amount / amount            -- 需 join ohlcv_1d
net_ratio        = net_mf_amount / amount
bucket_imbalance = (buy_x - sell_x) / (buy_x + sell_x)
```

> 注意：实测 Tushare 的 `net_mf_amount` 与四档净额不相等，二者是不同口径。派生指标必须显式声明用哪一个，不能混用。

### 5.3 分档源注册表：`moneyflow_source`

```sql
CREATE TABLE IF NOT EXISTS "moneyflow_source" (
    updated_at TIMESTAMP,
    source SYMBOL,
    vendor SYMBOL,
    dataset SYMBOL,
    unit STRING,
    definition STRING,         -- 分档阈值、方向判定、是否含集合竞价
    coverage_start TIMESTAMP,
    coverage_end TIMESTAMP,
    priority INT,              -- 越小越优先
    enabled BOOLEAN,
    notes STRING
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, source)
```

这张表解决「标明数据源」的两层含义：**行级 source**（这条数据是谁给的）+ **源级元数据**（它的口径是什么、覆盖到哪、优先级多少）。

### 5.4 概念：板块维度 `concept_board_dim`

```sql
CREATE TABLE IF NOT EXISTS "concept_board_dim" (
    ts TIMESTAMP,
    board_code SYMBOL,         -- 规范 ID，当前 = 同花顺 6 位代码
    name STRING,
    board_type SYMBOL,         -- concept / industry / region
    source SYMBOL,             -- akshare.ths_index
    source_code SYMBOL,        -- 源方代码
    status SYMBOL,             -- active / inactive
    updated_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(ts, board_code, source)
```

每天一份快照（375 行），保留名称/状态变更历史。

### 5.5 概念：板块日线 `concept_board_daily`

```sql
CREATE TABLE IF NOT EXISTS "concept_board_daily" (
    ts TIMESTAMP,
    board_code SYMBOL,
    board_name STRING,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume LONG,               -- 股
    amount DOUBLE,             -- 元
    pct_chg DOUBLE,            -- 小数
    source SYMBOL,
    fetched_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, board_code, source)
```

> 同花顺板块指数的 `volume/amount` 是成分股汇总，不是可交易 ETF 的量；回测只能当信号/基准，不能直接当成交标的。

### 5.6 概念：成分快照 `concept_member_snapshot`

```sql
CREATE TABLE IF NOT EXISTS "concept_member_snapshot" (
    ts TIMESTAMP,              -- 快照日（每次全量写同一 ts）
    board_code SYMBOL,
    stock_code SYMBOL,
    stock_name STRING,
    weight DOUBLE,             -- 可选，源不提供则 NULL
    in_date TIMESTAMP,         -- 源提供时填，否则 NULL
    source SYMBOL,
    fetched_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(ts, board_code, stock_code, source)
```

**PIT 查询**：取 `<= as_of` 的最近一个快照日，整组取，而不是按股票取 latest：

```sql
SELECT * FROM "concept_member_snapshot"
WHERE board_code = %s
  AND ts = (
      SELECT max(ts) FROM "concept_member_snapshot"
      WHERE board_code = %s AND ts <= %s
  )
```

这样「被剔除的股票」会随快照消失，不会残留。

### 5.7 概念：跨源别名 `concept_board_alias`

```sql
CREATE TABLE IF NOT EXISTS "concept_board_alias" (
    updated_at TIMESTAMP,
    board_code SYMBOL,         -- 规范 ID
    source SYMBOL,             -- akshare.em_board / tushare.ths_member
    source_code SYMBOL,
    source_name STRING,
    confidence DOUBLE,         -- 名称匹配置信度
    verified BOOLEAN,          -- 人工确认过
    notes STRING
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, board_code, source, source_code)
```

同花顺/东财/Tushare 的板块名与代码都不通用，**不要靠名称直接 join**；用别名表 + 人工确认。

### 5.8 同步状态

复用现有 `scheduled_task_state`（`scheduled_tasks/state.py`），新增 dataset：

| dataset | cursor 含义 | 批次 |
|---|---|---|
| `moneyflow` | 下一个待拉取的交易日 | 20 个交易日/心跳 |
| `concept_meta` | 无 | 全量一次 |
| `concept_daily` | 下一个待补的 board_code | 50 个板块/心跳 |
| `concept_member` | 下一个待抓的 board_code | 20 个板块/心跳 |

---

## 6. 数据源适配与 source 标注

### 6.1 Adapter 协议

```python
class MoneyflowSource(Protocol):
    source_id: str
    def fetch_by_date(self, trade_date: date) -> pd.DataFrame: ...
    def fetch_by_code(self, code: str, start: date, end: date) -> pd.DataFrame: ...

class ConceptSource(Protocol):
    source_id: str
    def list_boards(self) -> pd.DataFrame: ...
    def fetch_board_daily(self, board: Board, start: date, end: date) -> pd.DataFrame: ...
    def fetch_members(self, board: Board) -> pd.DataFrame: ...
```

所有 adapter 返回 **canonical 列 + 单位**，并自带 `source` 字段（由 service 统一写入，adapter 也可声明 `source_id`）。

### 6.2 源与优先级

| 数据集 | 主源 | 备源 | 现状 |
|---|---|---|---|
| 分档 | `tushare.moneyflow` | `akshare.em`（个股资金流） | 主源可用 |
| 板块列表 | `akshare.ths_index` | `tushare.ths_index`（5000 分） | 主源可用 |
| 板块日线 | `akshare.ths_index` | `tushare.ths_daily` / 成分聚合 | 主源可用 |
| 板块成分 | 见 §11 决策 | 同花顺网页 / 东财 / 手工 CSV | **待定** |

优先级放 `config.json`：

```json
{
  "moneyflow": {"source_priority": ["tushare.moneyflow", "akshare.em"]},
  "concept": {"source_priority": ["akshare.ths_index", "tushare.ths_index"]}
}
```

### 6.3 标注规则

1. 写入时由 service 统一填 `source` / `source_version` / `fetched_at`，adapter 不得自行拼字符串。
2. 查询返回结构里必须带 `data_source`（实际使用的源）和 `as_of`（数据日期）。
3. 回测 `run_id` 的 snapshot hash 纳入 `{source: version}` 集合；换源或换版本必须导致 run_id 变化。
4. 多源并存时按 `priority` 取一个；**同一序列不允许逐行混源**（会导致统计口径漂移）。

---

## 7. 同步编排

### 7.1 收盘后顺序

```text
15:05  concept_meta      板块列表（375 行，1 次调用）
15:10  concept_daily     板块日线（375 个板块，增量到最新交易日）
15:20  moneyflow         分档（按交易日，一次全市场）
15:40  concept_member    成分快照（按源能力决定频率）
```

### 7.2 分档回补

- 起始日默认 `2020-01-01`（与 `scheduled_tasks.daily_start_date` 对齐），可配置 `2010-01-01`。
- 一个交易日一次 `trade_date` 调用（实测 5577 行，未超 Tushare 单次 6000 行上限）。
- 2020–2026 约 1650 次调用；200 rpm 下约 10 分钟（不含重试）。
- 断点续传：`scheduled_task_state` 存 `cursor` = 已完成的最后交易日；`pending` 记录空数据日，重试上限 3 次。

### 7.3 概念日线回补

- 每个板块一次 `index_ths(symbol=name, start, end)`，返回该区间全部日线。
- 首次回补 375 次调用；之后每天增量（start = 该板块库内最后日期 + 1）。
- 限速：THS 非官方接口，建议 0.2–0.5s 间隔 + 指数退避；失败板块记 `pending`，下次重试。

### 7.4 成分快照

- 每次全量写同一 `ts`，保证 PIT 查询是「整组」语义。
- 频率：周频（默认）；被回测引用的板块可日频。
- 抓取预算：`max_boards_per_run`（默认 20），按 `board_code` 游标推进；全量一轮约 19 次心跳。

### 7.5 幂等与可见性

- 所有写入走 DEDUP，重复跑不产生重复行。
- QuestDB WAL 异步可见：写完立刻读可能读不到，沿用现有 `state.save` 的可见性等待逻辑；查询侧不要用「刚写完就读」做校验。

---

## 8. 查询、因子与回测接入

### 8.1 Repository / Service API

```python
concept.list_boards(as_of=None, status="active") -> DataFrame
concept.members(board_code, as_of=None) -> DataFrame
concept.daily(board_codes, start, end) -> DataFrame
concept.search(keyword) -> DataFrame

moneyflow.by_code(code, start, end, source=None) -> DataFrame
moneyflow.by_date(date, source=None) -> DataFrame
moneyflow.sources() -> DataFrame
```

### 8.2 MCP 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `get_concept_boards` | 无 | 板块列表 + `data_source` |
| `get_concept_members` | `board_code` / `board_name`、`as_of` | 成分股 + `snapshot_ts` + `data_source` |
| `get_concept_ohlc` | `board_code`、`start`、`end` | OHLCV + `data_source` |
| `get_moneyflow` | `code`、`start`、`end`、`source?` | 分档明细 + `data_source` |
| `get_moneyflow_rank` | `date`、`field`、`limit` | 横截面排名 |

### 8.3 因子

- `factors/flows.py`：`main_net_ratio`、`main_net_5d`、`bucket_imbalance`、`net_inflow_streak`、`mf_rank`。
- `factors/concept.py`：板块动量、板块成交额占比、板块内个股资金流聚合。
- 因子 `FactorMeta` 增加 `source` 字段（当前只有 `source=computed/stored`，需扩为含数据源）。

### 8.4 回测

- **分档**：`QuestDBDataSource.load(..., extra_datasets=("moneyflow",))`，按 `(date, symbol)` LEFT JOIN 进 `DataBundle.frame`，策略用 `F.custom(...)` 取列；不改变现有 `close/hfq_close` 口径。
- **概念**：新增 `ConceptDataSource.load_members(board, as_of)`，用于横截面 universe（板块轮动）与行业中性化；`universe="concept:xxx"` 作为显式 universe 的扩展。
- 首次成分快照之前的日期，标记 `membership_approximation=True`，与现有 `st_approximation` 同样的处理方式。

### 8.5 筛股（可选，二期）

`screener_daily` 不加分档列；如需按资金流筛股，走独立 `moneyflow_daily` join 的查询接口，避免 56 列宽表再膨胀。

---

## 9. 质量门禁

| 数据集 | 检查 | 处理 |
|---|---|---|
| moneyflow | 单日行数 < 4000 | `quality=partial`，记 pending |
| moneyflow | 四档买入合计 ≈ 成交额（±2%） | `quality=suspect`，记录 note |
| moneyflow | 重复 `(ts, code, source)` | DEDUP 兜底 + 计数告警 |
| moneyflow | 交易日缺口 | 与交易日历对账，补拉 |
| concept_daily | 板块 bar 数 vs 交易日历 | 缺口 > 5% 告警 |
| concept_daily | `volume<=0` 或 `amount<=0` | 标记 suspect，不参与因子 |
| concept_member | 单板块成分数较上次变动 > 30% | 暂不覆盖，人工确认（防抓取残缺） |
| concept_member | 未知股票代码（不在 `stock_basic_info`） | 记录并丢弃 |
| 全局 | 源覆盖率 / 最新日期 | 写入 `data_quality_daily`（可选） |

---

## 10. 分期与验收

| 阶段 | 交付 | 验收 | 人日 |
|---|---|---|---|
| P0 | 两张 schema + `init_database` / `migrate_database` 注册 | 空库可建表；存量库可迁移 | 0.5 |
| P1 | 分档 adapter + service + 同步 dataset + MCP | 2020 至今回补完成；`get_moneyflow` 返回 source | 2–3 |
| P2 | 概念元数据 + 板块日线 + MCP | 375 个板块列表/日线可查；缺口对账通过 | 2 |
| P3 | 成分源接入 + 快照 + PIT 查询 | `as_of` 可还原任意快照日成分 | 1–4（取决于源） |
| P4 | 因子 + 回测 join | `run_id` 含源版本；资金流因子可回测 | 2–3 |

**建议顺序**：P0 → P1 → P2 → P3 → P4。P1/P2 可并行，P3 先定源再开工。

---

## 11. 风险与待决

### 11.1 成分股源（必须拍板）

| 方案 | 成本 | 稳定性 | 覆盖 | 建议 |
|---|---|---|---|---|
| Tushare `ths_member`（升 5000 分） | 积分/费用 | 高 | 全 | **首选**，一次调用一个板块 |
| 东财 `stock_board_concept_cons_em` | 免费 | 中（本机被断） | 全 | 网络恢复后作主源/备源 |
| 同花顺网页抓取 | 免费 | 低（反爬、分页） | 全但慢 | 仅作兜底，限跟踪板块 |
| 手工 CSV | 低 | 高 | 固定集合 | 先跑通链路的临时方案 |

**待决**：是否升级 Tushare 积分？若不升级，先手工 CSV 导入 30–50 个重点板块，保证 P3/P4 能闭环。

### 11.2 其他风险

- **同花顺网页结构变更**：抓取器必须独立在 `sources/ths_scrape.py`，带选择器版本号与失败降级；不要把解析逻辑写进 service。
- **板块身份**：同花顺/东财/Tushare 代码与名称都不通用，必须走 `concept_board_alias`，禁止名称 join。
- **板块指数不可交易**：板块日线只作信号/基准；回测标的仍是个股。
- **分档口径不稳定**：Tushare `net_mf_amount` 与四档净额不一致（已实测），派生指标要固定口径并写进 `source_version`。
- **WAL 可见性**：同步完立刻校验会误报空数据，沿用现有等待逻辑。
- **磁盘与分区**：分档 2010–2026 约 1500 万行，月分区足够；成分快照按年分区，周频全量约 200 万行/年。

---

## 12. 附：与现有代码的迁移关系

1. 新表落地后，`concept_boards` / `concept_boards_ohlc` / `concept_constituents` 标记为 legacy，停止写入；读取路径切到新表。
2. `ConceptBoardsStrategy` / `ConceptBoardOhlcStrategy` 由新的 `ConceptService` 取代；`MarketDataLoader` 不再承担板块职责。
3. `MCP get_concept_boards` 保留签名，内部切新表；新增 `get_concept_members` / `get_concept_ohlc`。
4. `scheduled_tasks.DATASETS` 增加 `moneyflow` / `concept_meta` / `concept_daily` / `concept_member`，`run(dataset="all")` 串起来。
5. 现有测试补：schema 列、source 标注、PIT 查询、幂等、缺口重试。
