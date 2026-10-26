# Backtest 架构设计（v2.1）

> 目标：策略一个文件一个策略；因子库独立；数据源、输出、执行引擎都是可替换插槽；
> 单股与多股横截面共用一套回测管线。
>
> v2.0 的十项修正保留（策略工厂化、hfq 分块、公司行为、A 股规则、再平衡状态机、
> `run_id` 快照、optional extra、因果性测试、输出收敛）。
>
> **v2.1 依据 PyBroker 2.0.2 源码与契约测试，修正 6 处会静默影响收益口径的问题：**
>
> 1. **成交价**：`buy_fill_price` / `sell_fill_price` 是 `ExecContext` 属性，
>    默认 `PriceType.MIDDLE`；`StrategyConfig` 没有这两个字段。要“次日开盘成交”
>    必须逐单显式设 `PriceType.OPEN`，否则成交价是中间价。
> 2. **止损**：`_get_stops()` 只在 `buy_shares`/`sell_shares` 非空时生成止损，
>    否则 `to_result()` 抛 `ValueError`。横截面必须
>    `add_execution(risk_fn)` + `set_after_exec(rebalance_fn)`，不能只在 after_exec 风控。
> 3. **股数**：整手/T+1/涨跌停/费用一律由 `execution/sizing.py` 自己算，用
>    `buy_shares`/`sell_shares` 下单。`set_target_shares` 按当根 close 定价、
>    `max(int(n), 0)` 不整手，只保留作对照实验。
> 4. **指标口径**：`equity` 与 `metrics` 由**自建账本**产出，PyBroker 的 `metrics_df`
>    只作对照；公司行为降级为 `adj_factor` 近似 + `approximation=true`
>    （`screener_daily` 没有分红/送转明细）。
> 5. **stored 因子单位**：`screener_daily` 的 `price` 单位列（`atr14/ma20/close`）
>    是前复权锚点，与 hfq 现算差一个 per-symbol 常数，**禁止**走 stored；
>    只允许 `ratio/pct/bool/shares/days` 单位复用。
> 6. **范围**：新增 §0.5 MVP 里程碑与 §9.4 待验证假设清单；策略文件收敛到
>    `strategies/models/`，`list` 只扫描该目录。

---

## 0. 决策记录

| # | 决策 | 结论 |
|---|---|---|
| 1 | 回测引擎 | 只用 PyBroker 2.0.x；`EnginePort` 仅做隔离层 |
| 2 | 多股权重 | 可自定义；默认等权，内置等权/分数/波动率倒数/上限 |
| 3 | 再平衡 | `DAILY / WEEKLY / MONTHLY / EVERY_N_BARS / DRIFT`，默认 `WEEKLY` + buffer |
| 4 | 策略与因子 | 顶层 `strategies/` 与 `factors/` 两个独立包；`backtest/` 只做引擎 |
| 5 | 已落库因子 | `auto` 优先 `screener_daily`；覆盖/版本/单位不满足时整因子回退现算 |
| 6 | 复权口径 | 信号用 `hfq_close`；成交/整手/涨跌停用 `close`；`adj_factor` 驱动公司行为 |
| 7 | 数据读取 | 固定 anchor，允许分块；`DataBundle` 带 `snapshot` 与 `hash` |
| 8 | PyBroker 依赖 | optional extra `backtest`，锁定 `>=2.0.2,<2.1` |
| 9 | 输出 | 落 `runs / equity / orders / trades / selections`，positions 仅在内存或按需落 |
| 10 | 可复现 | `run_id` 覆盖策略、参数、因子、universe、数据快照、规则、版本、seed |

### 0.5 MVP 里程碑（先纵切，再铺开）

§1 原则 #10 要求先闭环。第一步只做“单股 + QuestDB + console”这一条纵切，约 10 个文件，
**不接 stored 因子、不做横截面、不做公司行为、不做 optimize**：

| # | 交付 | 验收 |
|---|---|---|
| 1 | `backtest/domain/models.py` + `errors.py` | `RunSpec/BacktestResult` 可构造 |
| 2 | `backtest/application/ports.py` | 四个 Protocol 可被 fake 实现 |
| 3 | `backtest/infrastructure/data/questdb.py` | 直出 `close + adj_factor`，500 只 × 5 年可加载 |
| 4 | `factors/{library,ops,panel}.py` | 因子对齐、无未来数据（§5.5） |
| 5 | `strategies/{base,context}.py` + `models/atr_trend.py` | 策略可 `validate()` |
| 6 | `backtest/infrastructure/engine/pybroker_engine.py` | 单股、`OPEN` 成交、`LONG_ONLY`、止损随开仓 |
| 7 | `backtest/infrastructure/output/console.py` | 打印 metrics / equity 摘要 |

MVP 的 Definition of Done：

- `python -m backtest run strategies/models/atr_trend.py --out console` 跑完并输出净值摘要；
- 同一 `run_id` 重跑结果一致；`execution_report` 为空或原因可解释；
- 成交价等于信号次日开盘价（§9.2 契约测试 #2 通过）。

延后到 MVP 之后：stored 因子（§5.4）、横截面（§6.3/§7）、公司行为（§8.5）、
`optimize/walkforward`（§9.1）、research（§12）。

---

## 1. 设计原则

1. **策略只依赖策略 API + 因子库**：不 import pybroker、QuestDB、data_interface，不做文件 IO。
2. **五个插槽**：数据源、universe、因子源、输出、引擎；全部 `Protocol + registry + 配置名`。
3. **横截面是通用情形，单股是特例**：universe 只有一只且不实现 `rank()` 时走单股路径。
4. **A 股规则横切**：T+1、涨跌停、整手、停牌、费用、公司行为统一在执行层，策略无感知。
5. **因子因果性优先**：时序算子只能用 `t` 及之前；横截面算子必须按日计算，禁止全样本统计。
6. **数据口径唯一**：`close` 不复权、`hfq_close` 后复权、`adj_factor` 累计因子；不下发“口径不明”的价格。
7. **可复现**：`run_id` 是结果的一部分；同输入必须同 id，同 id 必须幂等。
8. **指标只有一个来源**：`equity` / `metrics` 由自建账本产出；PyBroker 的 `metrics_df`
   只作对照，避免“两套数字打架”。
9. **先验证假设再写实现**：PyBroker 的行为用 §9.2 契约测试钉死，不靠读文档下结论。
10. **先闭环再扩张**：单股 + QuestDB + console/csv 先跑通，再做横截面、优化、研究。

---

## 2. 目录结构

```text
strategies/                       # 策略 API（纯 Python，不依赖 backtest）
├── __init__.py                   # 导出基类 / F / Universe / Rebalance / Weighting
├── base.py                       # BaseStrategy(ABC) / SingleSymbolStrategy / CrossSectionStrategy
├── context.py                    # StrategyContext / PanelView 协议
├── universe.py                   # UniverseSpec（策略侧声明）
├── weighting.py                  # Equal / Score / InverseVol / Capped
└── models/                       # 一文件一策略（`backtest list` 的唯一扫描目录）
    ├── _template.py
    ├── atr_trend.py              # 单股示例
    └── rank_mom20.py             # 横截面示例

factors/                          # 因子库（纯 pandas/numpy，不依赖 backtest）
├── __init__.py                   # F 命名空间
├── library.py                    # @factor / FactorDef / FactorRef / 注册表
├── ops.py                        # 时序算子，复用 screener.domain.factors + indicator
├── cross_section.py              # rank / zscore / winsorize / top_n / neutralize
├── panel.py                      # 长表 -> date×symbol 面板、warmup、快照切片
└── stored.py                     # screener_daily 列元数据（单位/版本/窗口）

backtest/                         # 只做「取数 -> 因子装配 -> 撮合 -> 输出 -> 研究」
├── __init__.py                   # 不 import pybroker
├── domain/
│   ├── models.py                 # RunSpec / BacktestResult / Selection / ExecutionReport
│   ├── errors.py
│   └── clock.py
├── application/
│   ├── ports.py                  # DataSourcePort / UniversePort / FactorSourcePort / OutputPort / EnginePort
│   ├── loader.py                 # 加载 strategies/models/*.py，校验 STRATEGY 是类/工厂
│   ├── registry.py               # DATA_SOURCES / UNIVERSES / OUTPUTS / ENGINES
│   └── runner.py                 # 管线编排（含 optimize / walkforward 分支）
├── infrastructure/
│   ├── data/                     # questdb / parquet / memory / composite
│   ├── factors/                  # questdb_store.py / computed.py / auto.py
│   ├── engine/pybroker_engine.py # 唯一 import pybroker 的地方
│   ├── execution/                # a_share.py / lots.py / corporate_actions.py / sizing.py
│   └── output/                   # console / csv / html / questdb / composite
├── research/                     # IC / RankIC / 分层 / 换手 / 衰减（不撮合）
├── config.py                     # BacktestSettings.from_dict
├── bootstrap.py                  # composition root
├── cli.py
└── __main__.py                   # python -m backtest run/list/validate/research

indicator/                        # 纯 pandas/numpy 指标（ATR 等），被 factors/ops.py 复用
screener/                         # 已有因子计算与 screener_daily 落库
data_interface/                   # 行情/财务/QuestDB/交易日历
```

依赖方向：

```text
strategies.*        -> strategies.base / strategies.context / factors.*
factors.*           -> screener.domain.factors / indicator.*     （纯计算，无 IO）
backtest.*          -> strategies.* / factors.*                   （单向）
backtest.infrastructure -> ports 实现（可 import data_interface / pybroker / QuestDB）
bootstrap           -> 唯一装配具体实现的地方
```

> `strategy/` 不再作为顶层包。`strategies/` 与 `factors/` 都是可独立复用的库，
> `backtest/` 只是消费者，避免 `strategy.strategies` 这种冗余命名。
>
> 策略发现规则：`backtest list` 只扫描 `strategies/models/*.py`，跳过 `_` 开头的文件；
> 每个文件必须定义模块级 `STRATEGY`（类或工厂），缺失或为实例时直接报错并指出文件名。
> 框架模块（`base/context/universe/weighting`）不在扫描范围内，避免被误当成策略。

---

## 3. 核心数据模型

### 3.1 长表 Schema

`DataSourcePort.load()` 返回 `DataBundle.frame`，列固定为：

| 列 | 口径 | 用途 |
|---|---|---|
| `symbol` | 6 位代码 | 主键 |
| `date` | 交易日（naive 北京时间） | 主键 |
| `open/high/low/close` | **不复权** | 成交、整手、涨跌停、盈亏 |
| `volume` | 股 | 量能因子 |
| `amount` | 元 | 流动性/容量约束 |
| `adj_factor` | 累计后复权因子 | 复权与公司行为 |
| `hfq_close` | `close × adj_factor` | **因子计算（默认）** |
| `adj_close` | `close × adj_factor / anchor` | 展示/兼容，可选 |

原则：

- **因子计算默认用 `hfq_close`**，它不依赖查询区间 anchor，天然可分块。
- `adj_close` 只用于展示或兼容旧因子；如果使用，anchor 必须在一次 run 内固定。
- 绝对价格不做跨股票比较；跨股票因子应使用收益率/比率（`pct_change`、`dist_*`、`atr_pct` 等）。

### 3.2 复权与公司行为

三层账本：

1. **执行账本**：`raw_shares`，用于整手、涨跌停、可卖数量。
2. **估值账本**：`equity = cash + Σ(raw_shares × close) + 公司行为调整`，**净值/指标以它为准**。
3. **公司行为账本**：检测 `adj_factor` 变化，按 `ratio = f_new / f_prev` 调整总收益。

**口径决定（v2.1）**：`BacktestResult.equity` / `metrics` 一律由估值账本产出；
PyBroker 的 `result.metrics_df` 只写进 `metrics_pybroker` 作对照，不参与对外指标。
理由：PyBroker 按不复权价 mark，不会把分红/送转补回来；若直接采用它的指标，
“补了公司行为”这件事就只停留在文档里。

公司行为的现实约束：

- `screener_daily` 只有 `adj_factor`，**没有**分红/送转明细，所以“按明细记现金/股数”
  在 v2.1 不会发生，实际只走近似；
- 近似无法区分“现金分红”和“送转股”，会把股本变化当成现金收益，送转比例大的票偏差明显；
- 因此必须 `meta["corporate_action_approximation"] = true`，并在 `corporate_actions`
  表里逐笔记录 `ratio`，便于事后用真实明细校验；
- MVP 阶段先**不实现**公司行为，只在 `meta` 里声明“未计入分红，长期收益偏保守”。

> 只用复权价做信号、却用不复权价算 PnL，是长期回测最常见的收益低估来源。
> v2.1 的处理是“自建账本 + 近似 + 显式标记”，精确分红/送转留到拿到明细数据之后。

### 3.3 数据快照与分块

```python
@dataclass(frozen=True)
class DataSnapshot:
    source: str
    max_ts: pd.Timestamp
    rows: int
    anchor_by_symbol: Mapping[str, float]
    hash: str                     # sha256(canonical(snapshot))[:16]
```

`DataSourcePort.load(symbols, start, end, warmup, freq)` 允许内部按日期/股票分块，但必须保证：

- 每个 symbol 的 `anchor_by_symbol` 在一次 run 内固定（优先 DB 最新因子）；
- `hfq_close` 与 `adj_factor` 来自同一数据快照；
- 返回的 `DataBundle.snapshot.hash` 纳入 `run_id`；
- 若使用 `adj_close` 且 anchor 来自 frame 末尾，则禁止分块，改为一次性加载并告警。

---

## 4. 插槽契约

### 4.1 数据源 `DataSourcePort`

```python
class DataSourcePort(Protocol):
    name: ClassVar[str]

    def load(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        warmup: int = 0,
        freq: str = "d",
    ) -> DataBundle: ...
```

`DataBundle`：

```python
@dataclass(frozen=True)
class DataBundle:
    frame: pd.DataFrame
    calendar: TradingCalendar
    snapshot: DataSnapshot
    universe: Universe
```

实现：`questdb`（默认）、`parquet`、`memory`（测试）、`composite`。

**QuestDB 实现要求**：

- 只读模式：回测运行期不回源；缺数据直接报错，由同步任务预补齐。
- `hfq_close` 用 `close * adj_factor` 现算，避免 qfq anchor 依赖。
- 代理 `MarketDataLoader` 时，显式关闭 `now/realtime`。

### 4.2 Universe `UniversePort`

```python
class UniversePort(Protocol):
    name: ClassVar[str]

    def resolve(self, as_of: date) -> Universe: ...

@dataclass(frozen=True)
class Universe:
    codes: tuple[str, ...]
    as_of: date
    fingerprint: str

    def is_member(self, code: str, day: date) -> bool: ...
```

- `all_a` / `index` / `explicit` / `file` 四种实现。
- `is_member` 必须是 point-in-time；若数据源只能给当前成分，`meta` 标记 `survivorship_bias=true`。
- `fingerprint` 纳入 `run_id`。

### 4.3 因子源 `FactorSourcePort`

```python
class FactorSourcePort(Protocol):
    name: ClassVar[str]

    def read(
        self,
        defs: Sequence[FactorDef],
        codes: Sequence[str],
        start: date,
        end: date,
    ) -> FactorFrame: ...

@dataclass
class FactorFrame:
    values: dict[str, pd.DataFrame]   # date × symbol
    meta: dict[str, FactorMeta]

@dataclass
class FactorMeta:
    name: str
    source: Literal["stored", "computed"]
    unit: Unit
    version: str
    warmup: int
    coverage_start: date | None
    coverage_end: date | None
    missing_rate: float
```

实现：

- `stored`：读 `screener_daily`，只做列映射/单位换算；
- `computed`：从 `DataBundle` 现算；
- `auto`：先尝试 stored，满足条件则用，否则**整因子**回退 computed；
- `composite`：只做按因子路由，不拼接同一因子的两个来源。

### 4.4 策略契约

策略不是“插槽实例”，而是**工厂/类**。loader 每次 run 实例化一次。

```python
class BaseStrategy(ABC):
    name: ClassVar[str]
    mode: ClassVar[Literal["single", "cross"]]
    params: ClassVar[Mapping[str, Any]] = {}

    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = {**type(self).params, **(params or {})}
        self.validate()

    @abstractmethod
    def factors(self) -> Sequence[FactorRef]: ...

    def hyperparams(self) -> Sequence[Hyperparam]:
        return ()

    def validate(self) -> None: ...
```

> `Protocol` 只用于 ports（结构性类型）；策略基类要能实例化、继承并复用 `__init__`/`validate`，
> 所以用 `ABC`。两者混用会让 `BaseStrategy(Protocol)` 既不能安全实例化、又误导类型检查。

单股：

```python
class SingleSymbolStrategy(BaseStrategy):
    symbols: Sequence[str]

    def on_bar(self, ctx: StrategyContext) -> None: ...
```

横截面：

```python
class CrossSectionStrategy(BaseStrategy):
    universe: UniverseSpec
    top_n: int = 30
    buffer: int = 15
    rebalance: Rebalance = Rebalance.WEEKLY
    weighting: Weighting = EqualWeight()

    def rank(self, panel: PanelView, dt: date) -> pd.Series: ...
    def weights(self, scores: pd.Series, selected: Sequence[str]) -> dict[str, float]:
        return self.weighting.apply(scores, selected)
    def on_bar(self, ctx: StrategyContext) -> None: ...  # 可选，仅风控
```

策略文件：

```python
# strategies/models/rank_mom20.py
from strategies import CrossSectionStrategy, Universe, Rebalance, EqualWeight
from factors import F

class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    universe = Universe.all_a(exclude_st=True, min_bars=260)
    top_n = 30
    buffer = 15
    rebalance = Rebalance.WEEKLY
    weighting = EqualWeight()

    def factors(self):
        return [F.mom(20, on="hfq_close")]

    def rank(self, panel, dt):
        return panel.at(dt, "mom20")

STRATEGY = RankMom20          # 类，不是实例
```

### 4.5 输出 `OutputPort`

```python
class OutputPort(Protocol):
    name: ClassVar[str]

    def emit(self, result: BacktestResult) -> list[OutputArtifact]: ...
```

`OutputArtifact`：

```python
@dataclass(frozen=True)
class OutputArtifact:
    kind: Literal["file", "table", "console"]
    path: Path | None
    table: str | None
    rows: int | None
```

内置：`console`、`csv`、`html`、`questdb`、`composite`。

### 4.6 引擎 `EnginePort`

```python
class EnginePort(Protocol):
    name: ClassVar[str]

    def run(self, request: EngineRequest) -> BacktestResult: ...

@dataclass(frozen=True)
class EngineRequest:
    spec: RunSpec
    data: DataBundle
    panel: FactorPanel
    universe: Universe
    calendar: TradingCalendar
    rules: ExecutionRules
    benchmark: pd.Series | None
    seed: int
```

`RunSpec` 至少包含：策略、参数、区间、频率、初始资金、再平衡、权重、输出、因子来源、fill 模式。

---

## 5. 因子库

### 5.1 因子定义

```python
Unit = Literal["ratio", "pct", "price", "currency", "shares", "days", "bool"]

@dataclass(frozen=True)
class FactorDef:
    name: str
    op: str
    args: tuple[Any, ...]
    inputs: tuple[str, ...]
    on: str = "hfq_close"
    window: int = 1                 # 预热窗口，warmup 由 max(window) 汇总，不再单列
    unit: Unit = "ratio"
    source: Literal["auto", "stored", "compute"] = "auto"
    stored_column: str | None = None
    version: str = "v1"
```

`warmup` 不单独存字段：`library` 汇总 `max(def.window)` 作为拉数前置量。
stored 因子的 `window=0`（库里行已吃过 lookback），见 §5.4 的 warmup 语义。

`FactorRef` 是策略侧的声明，`FactorDef` 是解析后的定义。

命名规则：

- `F.mom(20)` -> `mom20`；
- `F.atr(14)` -> `atr14`；
- `F.stored("atr14")` -> name=`atr14`，source=`stored`；
- 显式 `name=` 可覆盖；
- 重名且定义不同 -> 加载期报错。

单位：

| 单位 | 例子 | stored 换算 | 能否 stored |
|---|---|---|---|
| `ratio` | `mom20`, `beta_252` | 原值 | ✅ |
| `pct` | `pct_chg`, `atr_pct`, `volatility_20d` | 读库后 `/100` | ✅ |
| `price` | `close`, `ma20`, `atr14`, `high_52w` | 原值 | ❌ 见下 |
| `currency` | `total_mv`, `float_mv` | 原值，过滤器可用 | ✅ |
| `shares` | `volume`, `total_share` | 原值 | ✅ |
| `days` | `lookback_days` | 原值 | ✅ |
| `bool` | `is_new_high_52w` | `0/1` | ✅ |

**为什么 `price` 单位禁止 stored**：`screener_daily.close` 是**前复权**序列
（`screener/infrastructure/acquisition.py::_apply_qfq` 写入），`atr14 / ma20 / high_52w`
都在 qfq 上计算；而回测现算在 `hfq_close = close × adj_factor` 上。
两者相差一个 **per-symbol 常数**（前复权锚点 / 后复权基准之比），于是：

- 对单只股票，`stored atr14` 与 `computed atr14` 数值不等，§14 的“等价性测试”必然失败；
- 跨股票比较 `stored atr14` 时，比较的是“锚点缩放后的价格”，不是真实波动幅度。

所以 `price` 单位只走 `compute`。若确实要用库里的 `price` 因子，必须按
`anchor = hfq_close / close` 换算后再用，并在 `meta` 标注 `price_unit_rescaled=true`；
v2.1 不实现这条换算路径。

组合多个因子打分前，必须先 `rank`/`zscore` 归一化，禁止把 `pct` 与 `ratio` 直接相加。

### 5.2 时序算子

`factors/ops.py` 只做取列/清理/调用，不复制公式：

```python
from screener.domain.factors import pct_change, atr_wilder, realized_volatility, rolling_beta
from indicator.ATR import atr
```

> 当前 `indicator/ATR.py` 与 `screener/domain/factors.py` 各有一份 ATR 实现。
> 迁移第 0 步先合并为一份 canonical 实现（建议放 `indicator/ATR.py`，
> `screener/domain/factors.py` re-export），避免两套公式漂移。

### 5.3 横截面算子

`factors/cross_section.py` 全部**按日**计算：

```python
def rank_pct(panel: pd.DataFrame) -> pd.DataFrame: ...      # 每行排名
def zscore(panel: pd.DataFrame) -> pd.DataFrame: ...        # 每行均值/标准差
def winsorize(panel: pd.DataFrame, q: float = 0.01) -> pd.DataFrame: ...
def neutralize(panel, industry: pd.DataFrame, size: pd.Series | None = None): ...
def top_n(panel: pd.DataFrame, n: int) -> pd.DataFrame: ...
```

禁止：

- 对整列做全样本 `mean/std/quantile`；
- 用未来行业分类/未来市值做中性化；
- 用 `shift(-1)` 之外的未来数据。

### 5.4 stored / auto / compute

`auto` 使用 stored 的条件（全部满足）：

0. `unit ∈ {ratio, pct, bool, shares, days, currency}`；`price` 单位直接走 compute（§5.1）；
1. `formula_version` 与当前 `FactorMeta.version` 一致；
2. `first_date <= first_signal_date`；
3. `last_date >= last_signal_date`；
4. 区间内无缺口；
5. `is_partial = false`，`core_ready/full_ready` 满足；
6. 单位可换算，且换算后与 computed 的语义一致；
7. `screener_daily.computed_at` 不早于数据快照的 `max_ts`（否则告警/回退）。

**注意**：`first_date <= start + warmup` 是错误条件。stored 行在生成时已吃过 lookback，
所以只要求首个信号日有行；如果是从 raw 现算，才需要 `first_raw_date <= start - warmup`。

`auto` 回退时：

- **整个因子**回退，不允许同一因子部分 stored、部分 computed；
- `meta.factor_sources[name]` 记录 `source/version/coverage/missing_rate/fallback_reason`；
- `run_id` 包含最终来源，因此回退前后 id 不同。

尾部缺口（stored 只到 `T`、回测到 `T+k`）最常见：默认仍整因子回退，保证一次运行只有
一个来源。若某次回测确实需要省算力，可显式开 `--tail-compute`，在 `meta` 写
`factor_sources[name].seam = [T, T+k]`；v2.1 只实现默认路径，尾部续算留作后续优化。

### 5.5 因子因果性

强制测试：

```python
# 在 dt 截断的数据上算因子，与全量数据算出的历史值比较
full = build_panel(data_all)
truncated = build_panel(data_all[data_all.date <= dt])
assert_frame_equal(full.loc[:dt], truncated, check_exact=False)
```

所有横截面算子按日生效；所有时序算子只使用 `t` 及之前。

---

## 6. 策略 API

### 6.1 工厂与参数

- `STRATEGY` 必须是**类**或 `create_strategy()` 工厂，不能是实例。
- loader 每次 run 实例化：`strategy = STRATEGY(params=merged_params)`。
- `params` 用 dataclass/pydantic 校验；CLI 覆盖时创建新副本，不修改类属性。
- `hyperparams()` 返回 PyBroker `Hyperparam` 列表；没有则 `--optimize` 报错并提示。

### 6.2 单股

```python
class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ["600519"]
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"]), F.ma(20)]

    def on_bar(self, ctx):
        if ctx.pos == 0 and ctx.close > ctx.f("ma20"):
            ctx.target_weight(
                0.95,
                stop_loss=ctx.close - self.params["k"] * ctx.f("atr"),
            )
        elif ctx.pos > 0 and ctx.close < ctx.f("ma20"):
            ctx.close_position()
```

`ctx.target_weight(w)` 的语义：

- `w` 是**目标权重**（0~1，按 equity 计），不是“买入比例”；
- 已有持仓时，向上/向下调整到 `w`；
- `stop_loss` 是绝对价格；`stop_loss_pct`/`trailing_pct` 是百分比；
- 订单在**下一根 bar 开盘**成交：引擎必须逐单设 `ctx.buy_fill_price = PriceType.OPEN`
  （PyBroker 默认 `PriceType.MIDDLE`，不设就是中间价成交）。

实现映射（为什么不用 `set_target_shares`）：

| 步骤 | 做法 |
|---|---|
| 目标股数 | `sizing.target_shares(weight, equity, close, lot=100)` 自己算，向下取整 |
| 差额 | `delta = target - current`；先卖后买，卖出回款当日可用 |
| 下单 | `ctx.buy_shares = delta` / `ctx.sell_shares = -delta` |
| 止损 | 仅在**开仓那一根**随买入单设 `ctx.stop_loss / stop_loss_pct`，之后由 PyBroker 挂在 entry 上 |
| 成交价 | `ctx.buy_fill_price = ctx.sell_fill_price = PriceType.OPEN` |

不直接 `ctx.set_target_shares(w, dir="long")` 的原因（2.0.2 源码）：

- 它按**当根 close** 定价，而成交在下一根，跳空时股数系统性偏离；
- `calc_target_shares()` 返回 `max(int(shares), 0)`，**不做 100 股整手**；
- 不感知涨停/停牌/T+1 可卖数量，也不安排“先卖后买”的现金顺序。

`set_target_shares` 只保留给“无 A 股约束的对照实验”，不进入主路径。

持仓后的 ATR 追踪止损：`ctx.stop_trailing` 只对**新 entry** 生效，无法追加到已有持仓。
所以追踪止损必须自己在 `on_bar` 里判定并下卖单——这也是横截面策略仍需要
`add_execution(risk_fn)` 的原因（见 §9.1）。

### 6.3 横截面

```python
class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    universe = Universe.all_a(exclude_st=True, min_bars=260)
    top_n = 30
    buffer = 15
    rebalance = Rebalance.WEEKLY
    weighting = EqualWeight()

    def factors(self):
        return [F.mom(20, on="hfq_close")]

    def rank(self, panel, dt):
        return panel.at(dt, "mom20")
```

- `rank()` 返回 `Series[symbol, score]`，越大越优先；
- NaN 分数自动排除；
- `top_n` 不足时按实际数量持仓；
- `buffer` 只控制卖出阈值，不改变 `top_n`。

执行结构（不能省的一步）：

```python
strategy.add_execution(risk_fn, symbols)   # 逐 bar：on_bar 风控 + 开仓单（可设止损）
strategy.set_after_exec(rebalance_fn)      # 组合层：按目标权重下 buy/sell_shares
```

原因：PyBroker 的止损只能随**新开仓单**设置（`_get_stops()` 在 `buy_shares/sell_shares`
为空时抛 `ValueError`，见 §9.1）。若为了横截面方便只写 `add_execution(None, symbols)`，
`on_bar` 里的止损/追踪止损就没有落点。

`rebalance` 的日期门控优先用 PyBroker 的 interval（`add_execution(..., intervals=["weekly"])`
+ `ctx.interval("weekly")` 的 bar 数变化），比手写“周一/月初”判断更稳；`EVERY_N_BARS`
直接传 `intervals=[N]`。详见 §7。

### 6.4 上下文

| 读 | 写 |
|---|---|
| `symbol / dt / open / high / low / close / hfq_close` | `target_weight(w, stop_loss=...)` |
| `pos / cash / equity / weight / bars_held` | `buy_shares(n)` / `sell_shares(n)` |
| `f(name) / panel.at(dt, name)` | `close_position()` / `sell_percent(p)` |
| `rank / selected / universe_size / is_rebalance` | `set_stop(loss=, loss_pct=, trailing_pct=, hold_bars=)` |

`ctx` 是只读数据 + 订单意图；不暴露 PyBroker `ExecContext`。

### 6.5 权重

```python
class Weighting(Protocol):
    def apply(self, scores: pd.Series, selected: Sequence[str]) -> dict[str, float]: ...
```

内置：

- `EqualWeight`：`1/len(selected)`；
- `ScoreWeight`：按分数排名/归一化；
- `InverseVolWeight(cap=0.08)`：`1/vol` 归一化，单票上限；
- `CappedWeight(cap=0.1)`：迭代截断再归一化。

约束：

- 权重和默认 ≤ `max_gross_exposure`（默认 0.98），留现金缓冲；
- 单票上限默认 1.0（单股策略）；
- 权重为 NaN/0 的股票剔除；
- `allow_short=False`（A 股默认）。

---

## 7. 再平衡状态机

```text
每个 bar：
1. 先执行风控退出：价格止损、时间止损、ATR 追踪止损
2. 若 is_rebalance：
     scores = strategy.rank(panel, dt)
     selected = select(scores, top_n, buffer, current_positions)
     targets = strategy.weights(scores, selected)
     engine.set_targets(targets)
3. 否则：
     a. 若 buffer 触发（rank > top_n + buffer）：
          卖出该标的；默认不立即补仓
     b. 若 DRIFT 且 max(|current_weight - last_target|) > drift_threshold：
          按 last_targets 配平
4. 未成交目标（涨停/停牌/跌停）：
     记录 ExecutionReport；默认下一 bar 重试，直到下一调仓日
5. 收盘后记录 equity / selections / execution_report
```

语义：

- **非调仓日绝不重置目标权重**，否则会变成日频调仓；
- 风控退出优先于再平衡；
- **同一 bar 内固定“先卖后买”**：止损/剔除/超配卖出 → 用卖出回款 + 可用现金买入目标，
  顺序不能依赖字典遍历；
- buffer 卖出默认不立即补仓（`replace_on_buffer=False`），避免频繁交易；
- `DRIFT` 需要保存 `last_targets`；
- `EVERY_N_BARS` 按交易日历计数，不按自然日；
- 再平衡 ≠ 止损；两者独立。

实现建议：`DAILY / WEEKLY / MONTHLY / EVERY_N_BARS` 用 PyBroker 的压缩 interval
（`add_execution(..., intervals=[...])` + `ctx.interval(...)`）做门控，
`DRIFT` 仍用 `last_targets` 比较。手写“周一/月初”判断只在 interval 无法表达时兜底。

---

## 8. A 股执行规则

### 8.1 T+1 可用股数

维护：

```python
available_shares[symbol] = 昨日及以前买入、当前可卖的数量
today_bought[symbol] = 今日买入数量
```

每日开盘：

```text
available_shares += today_bought
today_bought = 0
```

卖出时 `qty = min(requested, available_shares[symbol])`。
多次买入、部分卖出、分红送转后都要更新台账。

### 8.2 涨跌停 / ST / 新股

- 主板 10%，ST/*ST 5%，创业板/科创板 20%，北交所 30%；
- 以 `prev_close` 计算涨跌停价，按最小价位取整；
- 涨停不买、跌停不卖；
- 新股上市前 5 日无涨跌幅（按配置 `new_listing_no_limit_days`）；
- ST 状态必须按日期取，不能用当前状态回填历史；
- 触板但未封死的近似口径：`close == limit_price` 视为不可成交（保守）。

### 8.3 停牌

- 当日无 bar：不下单，持仓按最近收盘估值；
- 复牌首日：按正常规则撮合；
- 停牌期间目标权重保留，复牌后按 `ExecutionReport` 重试。

### 8.4 费用

默认：

| 费用 | 默认 | 说明 |
|---|---|---|
| 佣金 | 0.00025，最低 5 元 | 双边 |
| 印花税 | **0.0005** | 仅卖出；2023-08-28 前为 0.001 |
| 过户费 | 0.00001 | 双边 |

配置支持日期分段：

```json
"stamp_tax_schedule": [
  {"effective_from": "1990-01-01", "rate": 0.001},
  {"effective_from": "2023-08-28", "rate": 0.0005}
]
```

### 8.5 公司行为（v2.1 只做近似）

现实约束：`screener_daily` 只有 `adj_factor`，**没有**分红/送转明细，所以“按明细记现金/股数”
在 v2.1 不会发生。

v2.1 的做法：

- 检测 `adj_factor` 变化，按 `ratio = f_new / f_prev` 调整估值账本；
- 该近似**无法区分**现金分红与送转股（都会表现为 factor 跳变），送转比例大的票偏差明显；
- 必须写 `meta["corporate_action_approximation"] = true`，并在 `corporate_actions`
  表中逐笔记录 `date / symbol / f_prev / f_new / ratio`；
- MVP 阶段可整体关闭（`corporate_actions="off"`），此时在 `meta` 声明
  “未计入分红，长期收益偏保守”，并在报告里提示。

精确处理（现金分红按公告日、送转按除权日调整股本）留到拿到分红明细数据之后，
不在 v2.1 范围内。

### 8.6 未成交与拒绝

`ExecutionReport`：

```python
@dataclass
class ExecutionReport:
    rejected: pd.DataFrame      # symbol, date, reason, requested, filled
    unfilled: pd.DataFrame      # symbol, date, target, actual, reason
    corporate_actions: pd.DataFrame
```

原因枚举：`limit_up`、`limit_down`、`suspended`、`t_plus_1`、`lot_size`、`cash`、`no_bar`、`price_invalid`。

---

## 9. PyBroker 引擎适配

### 9.1 映射表（按 2.0.2 源码逐条核实）

| 我们的概念 | PyBroker 2.0.2 | 备注 |
|---|---|---|
| 单股逐 bar | `add_execution(fn, symbols)` | 每 bar 每标的一次回调 |
| 横截面风控/开仓 | `add_execution(risk_fn, symbols)` | **必须**；止损只能随下单设置 |
| 组合调仓 | `set_after_exec(fn)`，收到 `dict[symbol, ExecContext]` | 只下 `buy_shares/sell_shares`，不能设止损 |
| 目标权重 | 我们算股数 → `ctx.buy_shares/sell_shares` | `set_target_shares` 仅作对照实验 |
| 仓位上限 | `set_max_long_positions(top_n)` | 与策略 `top_n` 一致 |
| 排名换仓（可选） | `ctx.long_score` + `enable_rotation(worst_rank_held=...)` | rotation 开启时 exec 里的订单被忽略 |
| 自定义权重（rotation） | `sizer=fn(RotationContext)`，`long_ranks[symbol]` | 与主路径二选一 |
| 止损 | `ctx.stop_loss / stop_loss_pct / stop_trailing / hold_bars` | 仅在 `to_result()` 存在订单时生成，否则抛 `ValueError` |
| 次日开盘成交 | `StrategyConfig(buy_delay=1, sell_delay=1)` + **逐单** `ctx.buy_fill_price = PriceType.OPEN` | 默认 `PriceType.MIDDLE` |
| 参数优化 | `pybroker.hyperparam` + `strategy.optimize(score_fn, sampler=..., n_trials=...)` | grid / tpe / random |
| Walkforward 优化 | `strategy.optimize(score_fn, windows=N)` | 每窗独立调参 |
| 固定参数多窗口 | `strategy.walkforward(windows=N, timeframe=...)` | 不做参数搜索 |
| 多周期 / 再平衡门控 | `add_execution(..., intervals=[...])` + `ctx.interval(...)` | weekly / monthly / 每 N bar |
| 只做多 | `StrategyConfig(position_mode=PositionMode.LONG_ONLY)` | 不必自定义 `allow_short` |
| 费用 | `StrategyConfig(fee_mode=FeeMode.* 或 Callable[[FeeInfo], Decimal])` | 自定义 callable 可做印花税；`FeeInfo.order_type` 已确认 |
| 收盘强平 | `StrategyConfig(exit_on_last_bar=True)` | 默认 False，会留持仓 |

三条红线（写错不会报错，只会让收益悄悄变样）：

1. **不设 `ctx.*_fill_price` 就是中间价**，不是开盘价；
2. **止损必须和下单在同一根 bar**，否则 `to_result()` 抛异常；已有持仓无法追加止损；
3. **`set_target_shares` 不做整手、按当根 close 定价**，不能用于 A 股调仓。

`set_target_shares` 的 `target` 定义确实是“可投资金比例”（equity × leverage，最大 1），
这条没错；错的是把它当成“能直接落 A 股规则”的接口。

### 9.2 版本与契约测试

```toml
[project.optional-dependencies]
backtest = ["lib-pybroker>=2.0.2,<2.1"]
```

契约测试落在 `tests/test_pybroker_contract.py`（未安装 pybroker 时整模块 skip，
与 `tests/test_backtest_data_source.py` 一致）。每个用例对应一条设计假设，
**失败就改设计，不改测试**：

| # | 用例 | 钉死的假设 |
|---|---|---|
| 1 | 默认成交价 | 不设 `buy_fill_price` 时成交价是中间价，不是 open |
| 2 | `PriceType.OPEN` | 设了之后成交价 = 信号次日 open |
| 3 | 止损必须随单 | `buy_shares=None` + `stop_loss_pct` 抛 `ValueError` |
| 4 | 止损随开仓 | 带 `buy_shares` + `stop_loss_pct` 能成交，stop 挂在 entry |
| 5 | `set_target_shares` 不整手 | 10% × 10 万 / 13.37 元 = 747 股（无 100 股取整） |
| 6 | `after_exec` 上下文 | fn 收到全部 symbol，且能下单 |
| 7 | `optimize(windows=3)` | 3 个 train/test 窗口都能跑 |
| 8 | `LONG_ONLY` | `sell_shares` 不产生裸空头（`sell_to_open` 不出现） |
| 9 | `FeeInfo.order_type` | 自定义费用 callable 能区分 buy/sell，可只在卖出收印花税 |

这些用例同时是 `pybroker_engine.py` 的验收标准：引擎改动后先跑它们，再跑策略。

**2026-10-26 实测：9/9 通过**（`lib-pybroker 2.0.2`、`numpy 2.4.6`、`pandas 2.3.3`）。
已确认：默认成交价 = `(high+low)/2`；`calc_target_shares(0.1, price=13.37, cash=100000) == 747`；
`stop_loss_pct` 单独设置抛 `ValueError`；`after_exec` 能拿到全部 symbol 并下单；
`optimize(windows=3)` 产出 3 个窗口；`FeeInfo.order_type` 为 `buy`/`sell`。

> `lib-pybroker` 已经是 optional extra。`uv sync`（不带 `--extra backtest`）不会安装 PyBroker，
> 契约测试自动 skip；回测 CLI 需要 `uv sync --extra backtest`。

### 9.3 现金与权重

- **归一化职责在 `weighting` 层**：`sum(weights) <= max_gross_exposure`（默认 0.98），
  超出按比例缩放；单票 `max_weight` 用“截断 + 迭代再分配”保证总和仍然成立；
- `set_target_shares` 不负责 0.98，PyBroker 只按 `buying_power` 裁剪，别把两件事混在一起；
- `allow_short` 直接用 `PositionMode.LONG_ONLY` 表达；
- 费用、整手、涨跌停导致的权重偏离记入 `ExecutionReport`，不静默忽略；
- 单票权重上限：单股默认 1.0，多股默认 0.1（可配）。

### 9.4 假设验证状态

前 9 条已由 `tests/test_pybroker_contract.py` 覆盖并全绿，可直接当作实现约束；
其余几条要在接入真实 QuestDB 数据时抽验。每条都对应一个可证伪的实验：

| 假设 | 验证方式 | 不成立时的对策 |
|---|---|---|
| 默认成交价是 `MIDDLE` | 契约 #1 | 本就显式设 `OPEN`，无影响 |
| `buy_delay=1` + `OPEN` = 次日开盘 | 契约 #2 | 改自定义 fill callable 或限价单 |
| 止损只能随下单 | 契约 #3/#4 | 自建止损：`sell_shares` 平仓 |
| `set_target_shares` 不做整手 | 契约 #5 | 本就自己算股数，无影响 |
| `after_exec` 能拿到全部 ctx 并下单 | 契约 #6 | 组合逻辑搬进 `exec_fn`（按 symbol 聚合） |
| `optimize(windows=N)` 语义 | 契约 #7 | 外层自己切窗 + `walkforward` |
| `FeeInfo` 能区分买卖方向 | 契约 #9（已确认 `order_type`） | 自建费用账本，成交后扣减 |
| `exit_on_last_bar=True` 在最后一根 bar 清仓 | 手工看 `orders` 末行 | 在 `sizing` 强制最后一日平仓 |
| `adj_factor` 是累计后复权因子 | 抽 3 只票，比较 `close×factor` 的收益率与 qfq 收益率 | 退回“固定 anchor + 禁分块”模式 |
| `screener_daily.close` 是前复权 | 抽 1 只票，比对 `close / raw_close` 与 `adj_factor` | 若为 raw，§5.1 的 `price` 规则要反转 |

顺序：`uv sync --extra backtest` → `pytest tests/test_pybroker_contract.py -q` → 再动手写引擎。

---

## 10. 运行管线与 run_id

```text
config.json + CLI
      │
bootstrap.resolve()            # strategy / data / universe / factors / outputs / engine / rules
      │
runner.run(spec)
      ├─ 1. load_strategy()            # importlib + 类/工厂校验 + 参数校验
      ├─ 2. universe.resolve(as_of)    # 显式 / 全A / 指数 / 文件
      ├─ 3. data = data_source.load()  # start-warmup .. end；固定 anchor；可分块
      ├─ 4. panel = factors.build()    # stored/auto/compute + 单位/覆盖/版本
      ├─ 5. engine.run(EngineRequest)  # 撮合 + A股规则 + 费用 + 公司行为 + 指标
      │       └─ optimize? -> strategy.optimize(score_fn, sampler, windows)
      ├─ 6. run_id = hash(...)         # 策略/参数/因子/universe/快照/规则/版本/seed
      └─ 7. outputs.emit(result)
```

`run_id` 负载：

```python
payload = {
    "strategy_hash": sha256(strategy_source),
    "params": canonical(params),
    "factors": canonical(factor_defs + factor_sources),
    "universe": universe.fingerprint,
    "dates": [start, end, freq],
    "data": data.snapshot.hash,
    "rules": canonical(rules),
    "fill": "next_open",                 # 决定是否显式设 PriceType.OPEN
    "corporate_actions": "approx | off",
    "metrics_source": "ledger",          # 恒为自建账本；PyBroker 指标只作对照
    "engine": {"name": "pybroker", "version": pybroker.__version__},
    "libs": {"numpy": np.__version__, "pandas": pd.__version__},
    "seed": seed,
    "code_version": APP_VERSION,
}
run_id = sha256(canonical(payload))[:16]
```

---

## 11. 输出与存储

`BacktestResult`：

```python
@dataclass
class BacktestResult:
    run_id: str
    spec: RunSpec
    metrics: dict[str, float]          # 自建账本产出，对外唯一口径
    metrics_pybroker: dict[str, float] | None   # 仅对照，不参与报告
    equity: pd.DataFrame               # date, cash, market_value, equity, drawdown
    orders: pd.DataFrame
    trades: pd.DataFrame
    selections: pd.DataFrame           # date, symbol, score, rank, weight, source
    corporate_actions: pd.DataFrame | None
    factor_meta: dict[str, FactorMeta]
    execution_report: ExecutionReport
    meta: dict[str, Any]              # 含 approximation / factor_sources / seam
```

`metrics` 与 `metrics_pybroker` 并列是刻意的：前者是我们对外的总收益口径（含近似公司行为），
后者是框架原始结果。两者差异过大时优先查执行层（费用、整手、未成交），
而不是调公司行为近似。

存储：

| 表 | 内容 | 键 |
|---|---|---|
| `backtest_runs` | run 元数据、metrics、meta | `run_id` |
| `backtest_equity` | 每日净值/现金/回撤 | `run_id, ts` |
| `backtest_orders` | 订单/拒绝/未成交 | `run_id, ts, symbol, order_id` |
| `backtest_trades` | 成交/费用 | `run_id, ts, symbol, trade_id` |
| `backtest_selections` | 每期排名/权重 | `run_id, ts, symbol` |

**不默认落全量 per-bar positions**；需要时按 `run_id` 从 `trades` 重建，或单独落持仓快照。

`OutputPort.emit()` 必须幂等：同 `run_id` 重复 emit 不产生重复行。

---

## 12. 因子研究

`backtest/research/` 复用同一份因子面板，不经过 PyBroker：

- `forward_returns(panel, horizons=(1, 5, 20))`：用下一期收益，最后 h 行为 NaN；
- `ic / rank_ic`：按日横截面相关，输出均值、t 值、胜率；
- `quantile_returns(panel, q=5)`：分层收益、多空组合；
- `turnover(panel, top_n)`：排名换手，预估成本；
- `decay(panel, lags)`：因子衰减。

入口：

```bash
python -m backtest research mom20 --start 2021-01-01 --end 2025-12-31 --out csv
```

研究层也要记录 `factor_meta` 和 `data.snapshot.hash`，保证与回测同口径。

---

## 13. 配置与 CLI

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
  "drift_threshold": 0.02,
  "rules": {
    "lot_size": 100,
    "t_plus_1": true,
    "limit_up_down": true,
    "suspension": true,
    "new_listing_no_limit_days": 5,
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

`warmup` 实际值 = `max(策略声明的因子 window, warmup_min_bars)`；stored 因子按 `window=0` 计
（库里行已吃过 lookback）。不要把它当成固定 260 天写死，否则短窗口因子白拉数据、长窗口因子不够用。

```bash
python -m backtest list
python -m backtest validate strategies/models/rank_mom20.py
python -m backtest run strategies/models/rank_mom20.py \
    --start 2021-01-01 --end 2025-12-31 --param top_n=30 --out csv,html
python -m backtest run strategies/models/rank_mom20.py --optimize grid --walkforward 3
python -m backtest run strategies/models/atr_trend.py --data parquet --out console
python -m backtest research mom20 --start 2021-01-01 --end 2025-12-31 --out csv
```

依赖安装：

```bash
uv sync --extra backtest
```

> MCP 运行时不需要 PyBroker 时，保持 `backtest` 为 optional extra；测试用 `pytest.importorskip("pybroker")`。

---

## 14. 测试矩阵

| 测试 | 目的 |
|---|---|
| 因子无未来数据 | 截断 vs 全量历史值一致 |
| stored vs computed 等价 | **仅 `ratio/pct/bool` 单位**；`price` 单位不在断言范围（§5.1） |
| price 单位禁止 stored | `F.atr()` 即使库里有 `atr14` 也必须走 compute |
| 因子单位换算 | pct/ratio/currency 不混用 |
| stored 覆盖/版本/partial | 不满足时整因子回退，meta 有记录 |
| 同一因子不得混源 | 禁止 stored + computed 拼接 |
| 策略加载 | 缺 `STRATEGY`、实例而非类、未知因子、参数越界 |
| 再平衡状态机 | 非调仓日不重置目标；buffer 只卖不补；drift 阈值 |
| 先卖后买 | 同 bar 目标调整时现金不出现负值，顺序稳定 |
| T+1 + 整手 | 多次买入/部分卖出/零股 |
| 涨跌停/停牌 | 涨停不买、跌停不卖、停牌不下单 |
| 成交价 | 信号次日成交价 == open（`PriceType.OPEN`） |
| 止损随单 | 无订单设 stop 抛错；有订单时 stop 挂到 entry |
| 公司行为近似 | `ratio` 调整后净值连续，`approximation=true` |
| 现金/费用 | 等权 10 只 + 费用不出现负现金 |
| 指标口径 | `metrics` 来自自建 equity；`metrics_pybroker` 仅对照 |
| PyBroker 契约 | `tests/test_pybroker_contract.py` 全绿（§9.2） |
| run_id | 同输入同 id；换因子来源/数据快照/fill 模式 id 变化 |
| 输出幂等 | 同 run_id 重复 emit 不重复 |
| 性能 smoke | 500 只 × 5 年日线在可接受时间内 |
| fake engine | 不依赖 PyBroker 的管线测试 |

---

## 15. 迁移步骤

0. **准备（半天，不写业务代码）**：
   - `uv sync --extra backtest`，确认 `pybroker.__version__ == 2.0.2`；
   - 写 `tests/test_pybroker_contract.py` 并跑绿——**先钉死 §9.4 的假设**；
   - 合并 `indicator/ATR.py` 与 `screener/domain/factors.atr_wilder` 为一份 canonical 实现；
   - 500 只 × 5 年内存/耗时 spike，决定是否要分块。
1. **MVP 纵切（§0.5）**：`domain` → `ports` → `questdb.py` → `factors`（现算）→
   `strategies/{base,template}` → `pybroker_engine`（单股 + `OPEN` + `LONG_ONLY`）→ `console`。
2. **MVP 验收**：`atr_trend` 跑通、`run_id` 稳定、成交价 == 次日 open、`execution_report` 可解释。
3. `execution/sizing.py` + `a_share.py`（T+1 / 整手 / 涨跌停 / 停牌 / 费用）+ `ExecutionReport`。
4. `stored` 因子（§5.4）：单位白名单、覆盖/版本校验、`meta.factor_sources`。
5. 横截面：`add_execution(risk_fn)` + `set_after_exec` + 权重/`buffer`/`drift` + `selections`。
6. 公司行为近似 + 自建 `equity/metrics` 账本 + `metrics_pybroker` 对照。
7. outputs（csv → html → questdb）+ `run_id` + `backtest_*` schema。
8. `optimize` / `walkforward` + `research/`。
9. CLI / bootstrap / 文档 / 测试收口。

第 2 步完成即可交付「单股 + QuestDB + console/csv」最小闭环；第 5 步完成交付多股横截面。

---

## 16. 已知风险

| 风险 | 处置 |
|---|---|
| PyBroker 2.x API 漂移 | 锁 `>=2.0.2,<2.1` + `tests/test_pybroker_contract.py` 全绿才升级 |
| 成交价默认中间价 | 逐单显式 `ctx.buy_fill_price = PriceType.OPEN`；测试断言 fill == open |
| 止损只能随下单 | 横截面必须 `add_execution(risk_fn)`；契约 #3/#4 守住 |
| `set_target_shares` 不整手 | 主路径不用它；sizing 自己算股数并配单测 |
| `screener_daily` 覆盖/版本不一致 | 整因子回退 + meta 记录；`--require-stored` 可硬失败 |
| `price` 单位跨锚点 | §5.1 白名单禁止 stored；等价性测试只覆盖 ratio/pct/bool |
| 单位混用 | `Unit` 枚举 + 换算测试 + 组合前归一化 |
| 同一因子混源 | 禁止；一次 run 一个来源；尾部缺口默认回退 |
| 复权 anchor/分块 | 因子用 `hfq_close`；`adj_close` 禁用分块；验证 `adj_factor` 累计性 |
| 公司行为 | 只有 `adj_factor`，只能近似；`approximation=true` + 逐笔记录；MVP 可关闭 |
| T+1/整手/涨跌停/停牌 | 执行层统一；`ExecutionReport` 记录原因 |
| 未成交目标 | carry 到下一 bar/下一调仓日；记录原因 |
| 再平衡变日频 | 状态机 + 非调仓日不重置目标 + golden test |
| 优化过拟合 | 默认 train/test + `windows` walkforward |
| 幸存者偏差 | universe as-of；退市股标注；静态 universe 时 `survivorship_bias=true` |
| 5000 只 × 10 年内存/性能 | 金额/现金/净值用 float64；仅因子面板可考虑 float32；按 universe/日期分块 |
| 输出过大 | equity + orders + trades；positions 按需 |
| 策略任意代码执行 | 只加载可信本地策略；文档声明 |

---

## 17. 附录：最小单股闭环

```python
# strategies/models/atr_trend.py
from strategies import SingleSymbolStrategy
from factors import F

class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ["600519"]
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"]), F.ma(20)]

    def on_bar(self, ctx):
        if ctx.pos == 0 and ctx.close > ctx.f("ma20"):
            ctx.target_weight(
                0.95,
                stop_loss=ctx.close - self.params["k"] * ctx.f("atr"),
            )
        elif ctx.pos > 0 and ctx.close < ctx.f("ma20"):
            ctx.close_position()

STRATEGY = AtrTrend
```

运行：

```bash
uv sync --extra backtest
python -m backtest run strategies/models/atr_trend.py \
    --start 2021-01-01 --end 2025-12-31 \
    --data questdb --out console,csv
```

产出：

```text
output/backtest/<run_id>/
├── metrics.json
├── equity.csv
├── orders.csv
├── trades.csv
├── selections.csv
└── execution_report.csv
```
