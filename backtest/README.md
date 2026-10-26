# Backtest（PyBroker）

> **设计以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准**（v2.1）。本文件只说明当前实现状态与用法。

## 状态

| 能力 | 状态 |
|---|---|
| QuestDB 只读数据源（`backtest/infrastructure/data/questdb.py`） | ✅ |
| 因子库 / 因子面板（`factors/`，compute） | ✅ |
| 策略 API / 加载器（`strategies/`、`backtest/application/loader.py`） | ✅ 单股；横截面 API 已就绪 |
| PyBroker 单股引擎（`pybroker_engine.py`） | ✅ |
| 自建账本 + `metrics`（`execution/ledger.py`） | ✅ |
| A 股规则（T+1 / 整手 / 涨跌停 / 停牌 / 费用） | ✅ |
| 输出 console / csv | ✅ |
| CLI（`python -m backtest list/validate/run`） | ✅ |
| `tests/test_pybroker_contract.py` | ✅ 9/9（PyBroker 2.0.2） |
| 横截面引擎（`add_execution(risk_fn)` + `set_after_exec`） | ⏳ 下一步 |
| stored 因子（`screener_daily`） | ⏳ 下一步 |
| 公司行为 / optimize / research | ⏳ 后续 |

当前 MVP 只跑**单股**。横截面策略文件可以加载和校验，但引擎会明确报错，避免静默跑错。

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

# 单股回测
python -m backtest run strategies/models/atr_trend.py \
    --start 2026-06-01 --end 2026-09-30 \
    --data questdb --out console,csv
```

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
- 成交价由引擎逐单设置 `PriceType.OPEN`，默认的 `MIDDLE` 不会出现在主路径；
- 止损只在开仓时随下单设置；已有持仓的追踪止损由策略自己判 `close_position`。

## A 股执行规则

`backtest/infrastructure/execution/` 统一处理：

- T+1 可用股数（`TPlusOneLedger`）；
- 100 股整手（买入向下取整；卖出允许零股）；
- 涨跌停：主板 10%、创业板/科创板 20%、北交所 30%，以 `prev_close` 计算；
  ST 5% 与新股无涨跌幅规则已实现但 MVP 未接历史 ST/上市日标记，默认按非 ST、非新股处理；
- 停牌：无 volume / 无有效 close 不下单；
- 费用：佣金（最低 5 元）、过户费、印花税（2023-08-28 起 0.0005，之前 0.001）；
- 未成交/拒绝写入 `ExecutionReport`，CSV 输出 `rejected.csv` / `unfilled.csv`。

## 指标口径

- `BacktestResult.metrics` 由自建 `SimpleLedger` 回放 `orders` + 每日 `close` 产出；
- `metrics_pybroker` 只作对照，不参与对外报告；
- 引擎会做 `ledger_reconciliation`，当前单股测试差异约 `1e-10`；
- MVP 公司行为关闭，长期收益会偏保守；`corporate_actions="approx"` 在后续接入。

## 依赖边界

- `strategies/`、`factors/` 纯 Python/pandas，不依赖 `backtest`；
- `backtest/` 依赖 `strategies` / `factors`；
- 只有 `backtest/infrastructure/engine/pybroker_engine.py` import `pybroker`；
- `backtest/__init__.py` 不 import pybroker，未安装时 `pytest.importorskip` 生效。
