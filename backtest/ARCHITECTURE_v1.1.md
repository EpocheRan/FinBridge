# Backtest 架构设计（v1.1）

> 目标：策略一个文件一个策略；数据源、输出、执行引擎都做成可替换的插槽；
> 单股策略与多股横截面策略共用一套接口，不写两套回测。
>
> v1.1 变更：策略与因子库提升到顶层 `strategy/`；引擎锁定 PyBroker 并补齐参数优化 /
> walkforward；权重可自定义；明确再平衡语义；复用 `screener_daily` 已落库因子。

## 0. 决策记录

| # | 决策 | 结论 |
|---|---|---|
| 1 | 回测引擎 | **只用 PyBroker**。参数优化、walkforward 走 `strategy.optimize()`；`EnginePort` 保留但只作为隔离层，不再计划自研撮合引擎 |
| 2 | 多股权重 | **可自定义**。`weights()` 钩子，默认等权，内置等权/分数加权/波动率倒数/权重上限 |
| 3 | 再平衡 | 支持 `DAILY / WEEKLY / MONTHLY / EVERY_N_BARS / DRIFT`，默认 `WEEKLY` + `worst_rank_held` 缓冲 |
| 4 | 策略目录 | **顶层 `strategy/`**：`strategy/strategies/` 放策略文件，`strategy/factors/` 放因子库 |
| 5 | 已落库因子 | **优先复用 `screener_daily`**，`auto` 模式：覆盖完整且 `formula_version` 一致才用，否则整因子回退现算 |

## 1. 设计原则

1. **策略只依赖「策略 API + 因子库」**，不 `import pybroker`、不 import QuestDB、不做文件 IO。
   好处：策略可以只用 pandas 单测、将来能接实盘/纸交易、换引擎不改策略。
2. **五个插槽**，全部通过 `Protocol` + 注册表 + 配置名解析：
   数据源、因子库、策略、输出、引擎。
3. **横截面是通用情形，单股是它的特例**（universe 只有一只、不实现 `rank()`）。
4. **A 股交易规则横切**（T+1、涨跌停、整手、停牌、费用）放在执行层，所有策略自动继承。
5. **复用已有资产**：`MarketDataLoader`、`screener/domain/factors.py`、
   `screener_daily` 因子表、`indicator/`、`TradingCalendar`、`config.json`、六边形分层。
6. **可复现**：`run_id = sha1(策略文件 + 参数 + 区间 + 关键配置 + 因子来源)`，随结果落盘。

## 2. 目录结构

```text
strategy/                        # 顶层：策略与因子库（不依赖回测/数据库/pybroker）
├── __init__.py                  # 导出 Strategy / F / Universe / Rebalance / Weighting
├── base.py                      # BaseStrategy / SingleSymbolStrategy / CrossSectionStrategy
├── context.py                   # PanelView / BarContext 抽象（引擎侧实现）
├── universe.py                  # UniverseSpec
├── weighting.py                 # Equal / Score / InverseVol / Capped
├── factors/
│   ├── __init__.py              # F 命名空间
│   ├── library.py               # @factor 注册表、FactorRef、参数与单位校验
│   ├── ops.py                   # 复用 screener/domain/factors.py + indicator/ATR.py
│   ├── cross_section.py         # rank / zscore / winsorize / top_n / neutralize
│   └── panel.py                 # 长表 -> date×symbol 面板、warmup 对齐、快照切片
└── strategies/                  # 一文件一策略
    ├── _template.py
    ├── atr_trend.py             # 单股示例
    └── rank_mom20.py            # 多股横截面示例

backtest/                        # 顶层：只做「取数 -> 撮合 -> 输出」
├── __init__.py                  # 不 import pybroker
├── domain/                      # RunSpec / BacktestResult / Selection / 错误
├── application/
│   ├── ports.py                 # DataSourcePort / OutputPort / EnginePort / UniversePort
│   ├── loader.py                # 按路径加载策略 .py，校验 STRATEGY
│   ├── registry.py              # DATA_SOURCES / OUTPUTS / ENGINES
│   └── runner.py                # 管线编排（含 optimize / walkforward 分支）
├── infrastructure/
│   ├── data/                    # questdb / parquet / memory / composite
│   ├── factors/                 # questdb_store.py：读 screener_daily 因子
│   ├── engine/pybroker_engine.py
│   ├── execution/               # a_share.py（规则）、sizing.py（权重->股数）
│   └── output/                  # console / csv / html / questdb / composite
├── research/                    # 因子研究：IC / RankIC / 分层 / 换手（不撮合）
├── config.py                    # BacktestSettings.from_dict
├── bootstrap.py
├── cli.py
└── __main__.py                  # python -m backtest run/list/validate/research
```

依赖方向：

```text
strategy.strategies -> strategy.base / strategy.factors       （纯 pandas）
strategy.factors    -> screener.domain.factors / indicator    （纯计算，无 IO）
backtest.*          -> strategy.*                             （单向，反向禁止）
backtest.infrastructure -> ports 实现（可 import data_interface、pybroker、QuestDB）
bootstrap           -> 唯一装配具体实现的地方
```

## 3. 插槽契约

### 3.1 数据源 `DataSourcePort`

```python
class DataSourcePort(Protocol):
    name: ClassVar[str]
    def query(self, symbols, start: date, end: date, freq: str = "d") -> pd.DataFrame: ...
```

长表列：`symbol, date, open, high, low, close, volume, adj_close`。

| 列 | 口径 | 用途 |
|---|---|---|
| `close` | 不复权 | 成交、整手、盈亏 |
| `adj_close` | 前复权 | 因子/信号 |
| `volume` | 股 | 量能因子 |

实现：`questdb`（默认）、`parquet`、`memory`（测试）、`composite`。
**硬约束**：一次取全区间（`start - warmup` 到 `end`）——复权锚点绑定取数区间，分段拼接会让前后因子不在同一坐标系。

### 3.2 因子库与因子来源

两层，避免和已有实现打架：

- **序列因子**：`strategy/factors/ops.py` 只做取列、清理 NaN/inf，公式引用
  `screener/domain/factors.py`（`pct_change / atr_wilder / rolling_beta / realized_volatility`）
  和 `indicator/ATR.py`，不复制公式。
- **横截面因子**：`cross_section.py`（`rank_pct / zscore / winsorize / top_n / neutralize`），
  作用在 `date × symbol` 宽表。

**优先用已落库因子（`screener_daily`）**，解析顺序由 `FactorRef.source` 控制：

| `source` | 行为 |
|---|---|
| `auto`（默认） | 先查 `screener_daily`；覆盖完整且版本一致就用，否则整个因子回退现算 |
| `stored` | 强制读库，缺数据直接报错（保证回测口径与筛股一致） |
| `compute` | 强制用 OHLCV 现算（研究新因子时用） |

`QuestDBFactorSource` 适配 `screener_daily` 的已有列（`atr14 / atr_pct / ma5..250 /
close_vs_ma* / volatility_20d|60d / beta_252 / corr_252 / volume_ratio_20d / turnover /
total_mv / float_mv / high_52w / dist_high_52w_pct ...`），三个必须处理的坑：

1. **单位**：库里 `pct_chg / atr_pct / volatility_* / close_vs_ma* / dist_*` 都是**百分数**（×100），
   现算因子是小数。`FactorDef.unit = "pct" | "ratio"`，读库后统一除以 100；混用单位是静默错误。
2. **覆盖**：`screener_daily` 只覆盖 screener 跑过的日期。用 `is_partial = false`、
   `core_ready/full_ready` 和「首条记录日期 ≤ start + warmup」判断；不满足就整因子回退现算。
3. **版本**：`formula_version` 必须与当前 `DailyFactorConfig.formula_version` 一致；
   不一致时默认回退现算并在 `meta` 里告警，可用 `--require-stored` 改成硬失败。

**同一因子在一次运行里只能来自一个来源**（全库或全算），否则同列不同口径会拼接出假信号。
`BacktestResult.meta["factor_sources"]` 记录每个因子最终用的来源、版本和缺失率。

注册与声明（策略里只声明、不计算）：

```python
# strategy/factors/library.py
@factor("mom", inputs=("close",), window=20, unit="ratio")
def mom(close: pd.Series, n: int = 20) -> pd.Series:
    return close.pct_change(n)

def factors(self):
    return [
        F.mom(20, on="adj_close"),   # 现算时序因子
        F.stored("atr14"),            # 库里已有列，强制读
        F.rank("mom20"),              # 横截面排名
    ]
```

`warmup_bars = max(window)` 由库汇总；未知因子/缺列/参数越界在**加载期**报错并列出可用因子。

### 3.3 策略（一文件一策略，顶层 `strategy/`）

```python
# strategy/strategies/atr_trend.py —— 单股
from strategy import SingleSymbolStrategy, F

class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ["600519"]
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"])]

    def on_bar(self, ctx):
        if ctx.pos == 0 and ctx.close > ctx.f("ma20"):
            ctx.buy_percent(0.95, stop_loss=ctx.close - self.params["k"] * ctx.f("atr"))
        elif ctx.pos > 0 and ctx.close < ctx.f("ma20"):
            ctx.sell_all()

STRATEGY = AtrTrend()
```

```python
# strategy/strategies/rank_mom20.py —— 多股横截面
from strategy import CrossSectionStrategy, Universe, Rebalance, EqualWeight

class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    universe = Universe.all_a(exclude_st=True, min_bars=250)
    top_n = 30
    buffer = 15                       # 跌出 45 名才卖
    rebalance = Rebalance.WEEKLY
    weighting = EqualWeight()         # 也可 ScoreWeight() / InverseVolWeight(cap=0.08)
    params = {"n": 20}

    def factors(self):
        return [F.mom(self.params["n"], on="adj_close")]

    def rank(self, panel, dt):
        return panel.at(dt, "mom20")  # index=symbol，越大越优先

    def on_bar(self, ctx):            # 可选：持仓期风控（每日）
        pass

STRATEGY = RankMom20()
```

策略可见的上下文（**只读数据 + 下单意图**，不暴露 pybroker 的 `ExecContext`）：

| 读 | 写 |
|---|---|
| `symbol / dt / open / high / low / close / adj_close` | `buy_percent(p, stop_loss=...)` |
| `pos / cash / equity / weight / bars_held` | `buy_shares(n)` / `sell_percent(p)` |
| `f(name)` / `panel.at(dt, name)` | `sell_all()` / `set_stop(loss_pct=, trailing=, hold_bars=)` |
| `rank / selected / universe_size / is_rebalance` | |

### 3.4 输出 `OutputPort`

```python
class OutputPort(Protocol):
    name: ClassVar[str]
    def emit(self, result: BacktestResult) -> Path | None: ...
```

`BacktestResult`：`run_id / spec / metrics / orders / trades / positions / portfolio /
selections / factor_stats / meta`。内置 `console / csv / html / questdb / composite`，
QuestDB 落 `backtest_runs / backtest_orders / backtest_trades`（表结构进 `scripts/schemas/backtest_schema.py`）。

### 3.5 引擎 `EnginePort`

```python
class EnginePort(Protocol):
    name: ClassVar[str]
    def run(self, spec, panel, strategy) -> BacktestResult: ...
```

PyBroker 的对应关系（已核实 2.x API）：

| 我们的概念 | PyBroker |
|---|---|
| 单股 / 逐 bar 信号 | `add_execution(fn, symbols)`，`ctx.long_pos()`、`ctx.buy_shares`、`ctx.sell_all_shares()` |
| 横截面调仓 | `add_execution(None, symbols)` + `set_after_exec(fn)`，fn 收到 `dict[symbol, ExecContext]` |
| 目标权重 | `ctx.set_target_shares(weight, dir="long")`（按权重差自动下单） |
| 仓位上限 | `set_max_long_positions(top_n)` |
| 排名换仓 | `ctx.long_score` + `enable_rotation(worst_rank_held=...)`（事件驱动模式，可选） |
| 自定义权重 | rotation 用 `sizer=fn(RotationContext)`；target-weights 模式直接算权重 |
| 止损 | `ctx.stop_loss_pct` / `ctx.stop_trailing` / `ctx.hold_bars` |
| 参数优化 | `pybroker.hyperparam` + `strategy.optimize(score_fn, sampler="grid"/"tpe"/"random", n_trials=...)` |
| Walkforward | `strategy.optimize(score_fn, windows=N)` |
| 并行 | `pybroker.enable_data_source_cache(...)`、进程级并行 |

**默认撮合路径是 target-weights**（`set_after_exec` + `set_target_shares`）：权重、缓冲、再平衡
全部可控，`rotation` 只作为「事件驱动换仓」模式保留。所有 PyBroker 细节收敛在
`infrastructure/engine/pybroker_engine.py`，策略层零感知。

## 4. 再平衡（Rebalance）

**作用**：决定「目标组合什么时候重算、什么时候调仓」。它不改信号，只改三件事：

1. **换手与成本**：日频调仓通常被佣金/印花税/冲击成本吃掉；周/月频显著省成本。
2. **信号衰减**：动量/量能类因子 5–20 日衰减，再平衡太慢会漏掉；太频会追噪声。
3. **持仓漂移**：涨得多的票权重变大，定期配平回到目标权重（风险控制）。

三种触发方式，可组合：

| 方式 | 语义 | 实现 |
|---|---|---|
| 日历 | 每周一/每月首个交易日重算目标 | 日期判断 + `set_target_shares` |
| 事件 | 排名跌出 `top_n + buffer` 立即卖 | 每日检查 `rank > worst_rank_held` |
| 漂移 | 权重偏离目标 > x% 才配平 | `ctx.weight` 与目标比较 |

默认：`WEEKLY` 重算目标 + 每日事件检查（缓冲 `buffer=ceil(top_n * 0.5)`）。
`EVERY_N_BARS` 用交易日历计数实现，适合分钟/周线回测。
**再平衡 ≠ 止损**：止损/时间止损是每日风控，与调仓频率无关，两者都在 `after_exec` 里先风控后调仓。

## 5. 单股 / 多股统一

| | 单股 | 多股横截面 |
|---|---|---|
| universe | `symbols = [...]` | `Universe.all_a(...)` / 指数成分 |
| 信号 | `on_bar(ctx)` 逐 bar | `rank(panel, dt)` 每期排名 |
| 撮合 | `add_execution(fn, symbols)` | `add_execution(None, symbols)` + `set_after_exec` |
| 下单 | `ctx.buy_percent / sell_all` | `ctx.set_target_shares(weight)` |
| 权重 | 单标的 0/1 | `weighting` 或自定义 `weights()` |
| 再平衡 | 不适用（信号驱动） | `rebalance` + `buffer` |

共享：数据源、因子面板、A 股规则、费用、输出、`run_id`、CLI。

## 6. A 股执行规则（横切）

`infrastructure/execution/a_share.py`，引擎下单前统一应用：

- **T+1**：按 `(symbol, buy_date)` 记录，当日买入禁止卖出；
- **涨跌停**：按板块（主板 10%、ST 5%、创业板/科创板 20%、北交所 30%）判断，涨停不买、跌停不卖；
- **整手**：买入向下取 100 股整数倍，卖出允许零股；
- **停牌**：当日无 bar 不下单，持仓估值沿用最近收盘；
- **费用**：佣金（双边，最低 5 元）、卖出印花税、过户费，全部进 `trades.fees`。

参数在 `config.backtest.rules`，测试可整体关闭。

## 7. 因子研究（不撮合）

`backtest/research/` 复用同一份因子面板，直接算研究指标：

- `forward_returns(panel, horizons=(1, 5, 20))`
- `ic / rank_ic`（按日横截面相关，输出均值、t 值、胜率）
- `quantile_returns(panel, q=5)`（分层收益、多空组合）
- `turnover(panel, top_n)`（排名换手，预估成本）
- `decay(panel, lags)`（因子衰减）

入口：`python -m backtest research <factor> --start ... --end ... --out csv`。
它和回测共用因子库与数据源，但**不经过 PyBroker**——这类面板统计用向量化 pandas 一遍算完，
比逐 bar 回调快两个数量级。参数搜索/走样本外仍用 PyBroker 的 `optimize`。

## 8. 运行管线

```text
config.json + CLI
      │
 bootstrap.resolve()            # strategy / data / factors / outputs / engine / rules
      │
 runner.run(request)
      ├─ 1. load_strategy()            # importlib + 校验 name/params/factors/rebalance
      ├─ 2. universe.resolve(as_of)    # 显式 / 全A / 指数 / 文件
      ├─ 3. data = data_source.query() # start-warmup .. end，一次取全
      ├─ 4. panel = factors.build()    # 库里优先，缺失回退现算；横截面因子后算
      ├─ 5. engine.run()               # 撮合 + A股规则 + 费用 + 指标
      │       └─ optimize? -> strategy.optimize(score_fn, sampler, windows)
      ├─ 6. run_id = hash(...)         # 含 factor_sources，保证口径可追溯
      └─ 7. outputs.emit(result)
```

时序：**T 收盘出信号，T+1 开盘成交**（`fill="next_open"`，可切 `close` 做敏感性）。
`PanelView.at(dt)` 按位置切片，物理禁止整列回看。

## 9. 配置与 CLI

```json
"backtest": {
  "engine": "pybroker",
  "data": "questdb",
  "factor_source": "auto",
  "outputs": ["console", "csv"],
  "output_dir": "output/backtest",
  "initial_cash": 1000000,
  "warmup_bars": 250,
  "fill": "next_open",
  "benchmark": "000300",
  "rebalance": "weekly",
  "rules": {
    "lot_size": 100, "t_plus_1": true, "limit_up_down": true, "suspension": true,
    "commission_rate": 0.00025, "min_commission": 5.0,
    "stamp_tax_rate": 0.001, "transfer_fee_rate": 0.00001
  }
}
```

```bash
python -m backtest list
python -m backtest validate strategy/strategies/rank_mom20.py
python -m backtest run strategy/strategies/rank_mom20.py \
    --start 2021-01-01 --end 2025-12-31 --param top_n=30 --out csv,html
python -m backtest run strategy/strategies/rank_mom20.py --optimize grid --walkforward 3
python -m backtest run strategy/strategies/atr_trend.py --data parquet --out console
python -m backtest research mom20 --start 2021-01-01 --end 2025-12-31 --out csv
```

## 10. 测试

- `memory` 数据源 fixture：合成 3 只票 × 300 天，全程离线；
- 因子契约：单位（pct vs ratio）、对齐、无未来数据、NaN 传播；
- 因子来源：`stored` 覆盖不足/版本不一致时必须回退且 `meta` 有记录；**同一因子不得混源**；
- 策略装载：缺 `STRATEGY`、未知因子、参数越界、`top_n` 与 `buffer` 冲突；
- golden test：3 只票手算排名，断言 `selections`、权重、下单顺序；
- 输出契约：文件/表存在、列名稳定、同 `run_id` 幂等；
- PyBroker 未安装时 `pytest.importorskip`；引擎测试用 fake engine，不依赖 pybroker。

## 11. 迁移步骤

1. 建 `strategy/` 包（base/context/universe/factors/strategies），先把 `screener.domain.factors` 接进 `ops.py`；
2. `strategy/factors/library.py` 注册表 + `panel.py`；
3. `backtest/domain` + `application/ports.py` + `config.py`；
4. `QuestDBFactorSource`（`screener_daily` 读库 + 覆盖/版本/单位校验）；
5. `pybroker_engine.py` 单股闭环（`add_execution`）；
6. `execution/a_share.py` + `sizing.py`；
7. 横截面闭环（`set_after_exec` + `set_target_shares` + 再平衡/缓冲/权重）；
8. outputs（console/csv → html/questdb）+ `run_id`；
9. `optimize/walkforward` + `research/`；
10. CLI、bootstrap、文档、测试收口。

第 5 步完成即可交付「单股 + QuestDB + console/csv」最小闭环。

## 12. 已知风险

| 风险 | 处置 |
|---|---|
| `screener_daily` 覆盖不全或版本不一致 | 整因子回退现算 + `meta` 记录；`--require-stored` 可硬失败 |
| 库里因子是百分数、现算是小数 | `FactorDef.unit` 统一换算，测试覆盖 |
| 同一因子混源拼接 | 禁止：一个因子一次运行只用一个来源 |
| 复权 anchor 与取数区间绑定 | 强制一次取全区间，校验覆盖 `start - warmup` |
| PyBroker `set_target_shares` 的权重口径（含现金/杠杆） | 引擎里单测：等权 10 只 + 10% 现金，断言下单股数 |
| 优化过拟合 | 默认 train/test 切分 + `windows=N` walkforward，报告分段指标 |
| 幸存者偏差 | universe 支持 as-of；退市股单独标注 |
| 5000 只 × 10 年面板 | 宽表 float64 百 MB 级；再大按 universe 分块算因子 |
