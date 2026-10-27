# Backtest（PyBroker）

> **设计以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准**（v2.1）。本文件只说明当前实现状态与用法。

## 状态

| 能力 | 状态 |
|---|---|
| QuestDB 只读数据源（`backtest/infrastructure/data/questdb.py`） | ✅ |
| 因子库 / 因子面板（`factors/`，compute） | ✅ |
| 策略 API / 加载器（`strategies/`、`backtest/application/loader.py`） | ✅ 单股 + 横截面 |
| PyBroker 单股引擎（`pybroker_engine.py`） | ✅ |
| 自建账本 + `metrics`（`execution/ledger.py`，多标的 `PortfolioLedger`） | ✅ |
| A 股规则（T+1 / 整手 / 涨跌停 / 停牌 / 费用） | ✅ |
| ST / 新股限制（当前 ST 状态 + 上市日） | ✅ 近似（历史 ST 未接） |
| 横截面引擎（explicit universe + 候选集混合架构） | ✅ |
| pending target 重试 / buffer 容量分配 | ✅ |
| benchmark（`000300`）与 alpha/beta/tracking error | ✅ |
| 公司行为 `approx`（从 `adj_factor` 推导） | ✅ 近似 |
| 输出 console / csv | ✅ |
| CLI（`list/validate/run/research/optimize`） | ✅ |
| 优化 / walk-forward（网格，完整回测路径） | ✅ |
| 因子研究（IC / 分层 / 换手 / 衰减） | ✅ |
| `tests/test_pybroker_contract.py` | ✅ 9/9（PyBroker 2.0.2） |
| 横截面 all_a / index / file universe | ⏳ 后续（需 PIT 成分/ST 历史） |
| stored 因子（`screener_daily`） | ⏳ 下一步 |

当前横截面只支持 **explicit universe**；`all_a/index/file` 会明确报错，不会静默跑错。
横截面是“混合架构”：向量化层负责全 universe 排名/选股/权重，PyBroker 只执行候选集
（所有调仓日入选标的的并集）。见 `backtest/application/cross_section.py`。

横截面执行约定：

- `risk_fn` 只对**持仓**调用 `strategy.on_bar`；未持仓标的由 `after_exec` 统一开仓，
  所以横截面策略不要在 `on_bar` 里买；
- 不使用 PyBroker 的 `stop_loss/stop_trailing`（入场在 `after_exec`，无法绑定 entry）；
  风控退出在 `on_bar` 里 `close_position()`，`meta["stops"]="strategy_managed"`；
- 非调仓日只做 buffer 退出（`rank > top_n + buffer`），不重置目标权重；
- 调仓日先卖后买，卖出回款计入买入预算（PyBroker 默认卖先于买，已用契约实验验证）。

## 安装

PyBroker 是 optional extra，MCP 运行时不需要它：

```bash
uv sync --extra backtest
```

## 快速开始

```bash
# 列出策略
python -m backtest list

# 校验策略文件
python -m backtest validate strategies/models/atr_trend.py

# 单股回测（默认 benchmark=000300）
python -m backtest run strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --out console,csv

# 网格优化；加 --windows 2 即 walk-forward
python -m backtest optimize strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --grid k=1.0,2.0,3.0 --metric sharpe

python -m backtest optimize strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --grid k=1.0,2.0 --windows 2

# 因子研究（IC / 分层 / 换手 / 衰减）
python -m backtest research --factor mom20 \
    --codes 600519,000001,600036 \
    --start 2026-06-01 --end 2026-09-30 --data questdb
```

`corporate_actions=approx` 时输出还包含 `corporate_actions.csv`；该文件由
`adj_factor` 变动推导，现金分红被当作再投资，`meta["corporate_action_approximation"]`
会标记为 `true`。

输出目录默认 `output/backtest/<run_id>/`：

```text
metrics.json
equity.csv
orders.csv
trades.csv
selections.csv
rejected.csv
unfilled.csv
corporate_actions.csv
```

## 数据口径

| 列 | 口径 | 用途 |
|---|---|---|
| `close` | 不复权 | 成交、整手、涨跌停、盈亏 |
| `hfq_close` | `close × adj_factor` | **因子计算（默认）** |
| `adj_close` | `close × adj_factor / anchor` | 展示/兼容 |
| `adj_factor` | 累计后复权因子 | 复权与公司行为 |
| `volume` | 股 | 量能因子 |

- 运行期只读 QuestDB，不回源；缺数据由同步任务预补齐。
- 因子在 `hfq_close` 上计算，避免前复权 anchor 依赖，允许分块。
- 策略做价格趋势比较时用 `ctx.hfq_close`，不要拿 raw `ctx.close` 和 hfq 均线比。
- `backtest/data_questdb.py` 是旧的 PyBroker `DataSource` 适配器，保留兼容；新管线用
  `backtest/infrastructure/data/questdb.py`。

## 策略写法

```python
# strategies/models/atr_trend.py
from strategies import SingleSymbolStrategy
from factors import F

class AtrTrend(SingleSymbolStrategy):
    name = "atr_trend"
    symbols = ("600519",)
    params = {"n": 14, "k": 2.0}

    def factors(self):
        return [F.atr(self.params["n"]), F.ma(20)]

    def on_bar(self, ctx):
        ...

STRATEGY = AtrTrend          # 类/工厂，不能是实例
```

重点：

- 策略不 import pybroker / QuestDB / data_interface；
- 策略每次 run 由 loader 实例化，避免 optimize/walkforward 状态泄漏；
- `target_weight` 是目标权重，不是“买入比例”；
- 成交价由引擎逐单设置：`fill=next_open` → `PriceType.OPEN`，`fill=close` → `PriceType.CLOSE`；
  PyBroker 默认的 `MIDDLE` 不会出现在主路径；
- 止损只在开仓时随下单设置；已有持仓的追踪止损由策略自己判 `close_position`。

## A 股执行规则

`backtest/infrastructure/execution/` 统一处理：

- T+1 可用股数（`TPlusOneLedger`）；
- 100 股整手（买入向下取整；卖出允许零股）；
- 涨跌停：主板 10%、创业板/科创板 20%、北交所 30%，以 `prev_close` 计算；
  主板 ST 5%，创业板/科创板 ST 20%，北交所 30%；新股前若干交易日无涨跌幅；
  规则已接线：`stock_basic_info.is_st` 提供当前 ST 状态，`list_date` 提供上市日；
  历史 ST 仍缺 point-in-time 明细，因此 `meta["st_approximation"]=true`；
- 停牌：无 volume / 无有效 close 不下单；
- 费用：佣金（最低 5 元）、过户费、印花税（2023-08-28 起 0.0005，之前 0.001）；
- 未成交/拒绝写入 `ExecutionReport`，CSV 输出 `rejected.csv` / `unfilled.csv`。

## 指标口径

- `BacktestResult.metrics` 由自建 `SimpleLedger` / `PortfolioLedger` 回放 `orders`
  + 每日 `close` 产出；`metrics_pybroker` 只作对照，不参与对外报告；
- 引擎会做 `ledger_reconciliation`，当前测试差异约 `1e-10`（单股）/ `1e-8`（横截面）；
- benchmark 来自 `ohlcv_1d` 的 `market='IDX'`：`000300` 可产出
  `beta / alpha_annualized_pct / tracking_error_pct / information_ratio /
  benchmark_return_pct / excess_return_pct`；benchmark 缺失时这些键不存在，
  `meta["benchmark_used"]=false`；
- `corporate_actions="approx"`：按 `adj_factor` 变动生成 `share_ratio`，总收益连续，
  但不区分现金分红/送转；`off` 时不计公司行为；`exact` 仍 fail-fast；
- 同样 fail-fast 的还有：`allow_short=true`、`factor_source=stored`、非法 `fill`、
  `max_gross_exposure ∉ (0,1]`、未知配置键。

## 优化与 walk-forward

`backtest/application/optimize.py` 采用**完整回测网格搜索**：每个参数组合都走
`BacktestRunner.run`，因此复用 PyBroker 执行、A 股规则、账本和 benchmark。
这也意味着参数可以影响因子（例如 `n`），因为每个 trial 会重建因子面板。

- `runner.optimize(..., grid={"k": [1, 2, 3]}, metric="sharpe")`：全样本网格；
- `runner.walkforward(..., windows=3, train_size=0.6, lookahead=1)`：逐窗口在
  train 上选参，在 test 上回测；返回每个 fold 的 best_params / test metrics，
  顶层 `best_score` 是各 test 窗口 metric 的均值（不在 test 上二次挑窗口）；
- 网格应保持小；没有缓存，trial 数 × 数据加载/因子构建成本；
- 窗口边界按工作日切分，不读取交易所节假日表；正式研究应换成交易日历切分。

## 因子研究

`backtest/research/` 不经过 PyBroker：

- `forward_returns(close, horizons)`：date × symbol 的未来收益；
- `ic(factor, forward)` / `ic_summary`：默认 rank IC（无 scipy 依赖）；
- `quantile_returns(factor, forward, q)`：分层收益 + long-short；
- `turnover(factor, top_n)`：Top-N 换手；
- `decay(factor, close, horizons)`：不同持有期的 IC 衰减；
- CLI `research` 支持 `mom/ma/atr/vol` + 窗口，如 `mom20`、`atr14`；
  CSV 写到 `output/backtest/research/<factor>/`。

## 已知限制

- 横截面 universe 仍只支持 `explicit`；`all_a/index/file` 未实现；
- 历史 ST 未接 point-in-time 数据：当前用 `stock_basic_info.is_st` 近似并标记；
- 公司行为 `approx` 把分红当再投资，不能给出真实现金流/送转明细；
- pending target 只重试到下一个调仓日；buffer 容量分配已做，但 `selections`
  仍记录原始目标权重，不记录缩放后的 effective weight；
- 优化/研究未做并行程，参数网格较大时会慢。

## 依赖边界

- `strategies/`、`factors/` 纯 Python/pandas，不依赖 `backtest`；
- `backtest/` 依赖 `strategies` / `factors`；
- 只有 `backtest/infrastructure/engine/pybroker_engine.py` import `pybroker`；
- `backtest/__init__.py` 不 import pybroker，未安装时 `pytest.importorskip` 生效。
