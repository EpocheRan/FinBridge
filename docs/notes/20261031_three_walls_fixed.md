# 三堵墙的处理结果（2026-10-31）

> 承接 `20261031_first_strategy_turnover_regime.md` 里记的三个回测层缺口。
> 结论：三堵墙全部打通，且过程中发现一件比「修 bug」更重要的事——
> **冠军因子是小盘信号，原先 8.23% 的基线里含幸存者偏差与池子效应。**

| 墙 | 状态 | 关键改动 | 证据 |
|---|---|---|---|
| 只支持 explicit universe | ✅ | PIT 成分解析 + 逐日过滤 + 指纹进 run_id | CSI300 同一因子从 +8.2% 变 -2.5% |
| 回测不取 `turnover` | ✅ | 按表名补列；缺列补 NaN | 冠军策略从「空计划」变可运行 |
| 面板只有 OHLCV | ✅ | 两融/资金流 left join + 5 个 sentiment 因子 | 200 池 Sharpe 0.537 → **0.757** |

---

## 1. 墙一：股票池只有 explicit

### 改了什么

新增 `backtest/application/universe.py`：

- `MembershipSnapshot`：某一期成分快照；
- `UniverseSchedule`：`codes`（取数并集）+ `snapshots`（按期成分），
  `members_at(dt)` 取「dt 之前（含）最近一期」——**PIT，且 fail closed**：
  首期快照之前返回空集合，宁可不交易也不用未来成分；
- `fingerprint()`：池子 + 快照序列的哈希，进入 `run_id`，指数调仓后自动换 run。

`backtest/infrastructure/data/questdb.py` 新增 `resolve_universe(spec, start, end)`：

| kind | 做法 |
|---|---|
| `explicit` | 原样返回 |
| `index` | 读 `index_weight`，每期一条快照；取数代码 = 各期并集（含 start 前 500 天最近一期） |
| `all_a` | 区间内有行情的全部 `type='stock'`，可选剔除 ST；>3000 只时告警 |

`runner` + `cross_section` 接入：逐日按成分过滤排名，非成分当天不参与排名、
也不会被 buffer 规则保留；`_prepare` 用快照指纹替换 `data.universe`，进入 `run_id`。

### 同一因子、只换池子

策略/因子/参数完全相同（`liquidity.turnover_std_bias_42_252`，月度、top30、等权、
2020-01 ~ 2026-10）：

| 池子 | 年化 | Sharpe | 最大回撤 | 说明 |
|---|---|---|---|---|
| 固定 200 池（2017-11 成交额） | **+8.23%** | **0.537** | -23.2% | 入池日固定、退市缺失 → 幸存者偏差 |
| 沪深300（PIT） | **-2.48%** | **-0.10** | -33.8% | 大盘股上失效 |
| 中证500（PIT） | +3.59% | 0.30 | -24.1% | 勉强正收益 |
| 中证1000（**成分冻结在 2019-06**） | +12.97% | 0.82 | -22.8% | 小盘有效，但非 PIT，只能参考 |

**读法**：因子不是「无效」，也不是「8% 稳赚」，而是**小盘换手状态信号**。
把它用在沪深300 上会亏钱，用在固定 200 池上是把幸存者偏差算进了收益。
这也解释了为什么它在 200 只池子上的 IC 看着漂亮、一进大盘股就消失。

### 数据缺口（已加告警）

`index_weight` 的快照覆盖不齐：

| 指数 | 快照期数 | 区间 |
|---|---|---|
| 000300.SH / 000905.SH / 000016.SH | 172 | 2010-01 ~ 2026-09 ✅ |
| 000852.SH（中证1000） | 47 | 2014-10 ~ **2019-06** ❌ |
| 000001.SH（上证指数） | 61 | 2011-05 ~ **2017-05** ❌ |

`_resolve_index` 现在会在「最新快照早于回测终点 400 天」时打印 WARNING：
该段区间按**冻结成分**处理，结果会失真。补 000852 的 2020 以后成分是下一项数据任务。

---

## 2. 墙二：回测不取 `turnover`

**根因链**：`questdb.py` 的 SELECT 只取 `open/high/low/close/volume/amount/adj_factor`
→ `_turnover()` 退回 `volume / float_share` → `float_share` 也不在帧里
→ 换手类因子整列 NaN → 调仓计划为空 → 「没有可执行的调仓目标」。

**修法**：按表名拼列（`ohlcv_1d/ohlcv_1w` 有 turnover，分钟线没有），
缺列时显式补 NaN 列，保证 `_turnover()` 至少拿到一列而不是 KeyError。

**教训**：报告里的 B 级 ≠ 回测可用。建议在 `factor_audit` 里加一条
「因子输入列是否在回测帧里」的静态检查，把这类问题挡在跑回测之前。

---

## 3. 墙三：面板只有 OHLCV

### 数据通道

`load(..., extras=("margin_detail", "moneyflow_daily"))` 按 `(symbol, date)`
左连接辅助表，列名规范化：

| 数据源 | 面板列 |
|---|---|
| `margin_detail` | `margin_rzye` / `margin_rzmre` / `margin_rqye` / `margin_rqyl` |
| `moneyflow_daily` | `mf_buy_sm` / `mf_sell_sm` / `mf_buy_lg` / `mf_sell_lg` / `mf_buy_elg` / `mf_sell_elg` / `mf_net_amount` |

runner 从因子定义反推 extras（`_extras_for`），只查真正需要的表；
辅助数据读取失败只告警、不让回测挂掉（相关因子退化为 NaN）。

### 因子与注册

`factors/ops/flow.py` 新增 5 个算子（列缺失返回全 NaN，不抛错），
`factor_engine/definitions/flow.py` 注册进 `sentiment` 族：

| factor_id | 公式 | 方向 |
|---|---|---|
| `sentiment.margin_balance_chg_20d` | 融资余额 20 日变化率 | -1 |
| `sentiment.margin_short_ratio` | 融券余额 / 融资余额 | -1 |
| `sentiment.margin_buy_ratio_20d` | MA20(融资买入额 / 成交额) | -1 |
| `sentiment.flow_large_net_ratio_20d` | MA20(大单+特大单净买入 / 成交额) | +1 |
| `sentiment.flow_retail_net_ratio_20d` | MA20(小单净买入 / 成交额) | -1 |

`F.ts(...)` 现在同时支持价量与情绪算子，策略侧写法不变。

### 合成效果（固定 200 池，其余参数相同）

| 策略 | 年化 | Sharpe | 最大回撤 | 波动 |
|---|---|---|---|---|
| 冠军单因子 | 8.23% | 0.537 | -23.2% | 17.6% |
| **冠军 + 两融（rank 等权）** | **11.94%** | **0.757** | **-20.2%** | 16.8% |

收益 +3.7pct、Sharpe +0.22、回撤收窄 3pct——与探针里「IC +22%、净价差翻倍」的方向一致。

**但不要外推**：同一合成在中证1000（冻结成分）上是 12.97% → 12.36%、Sharpe 0.825 → 0.750，
**没有增益**。两融因子的价值依赖池子：在小盘/中盘混合池里补上了冠军缺的维度，
在纯小盘池里则与冠军重叠。这本身是下一轮因子设计要回答的问题。

---

## 4. 测试

- 新增 `tests/test_backtest_universe.py`（PIT 语义、指纹、计划过滤）与
  `tests/test_flow_factors.py`（算子降级、口径、注册元数据），共 12 例；
- 全库：**465 passed / 12 skipped / 2 failed**；
- 2 个失败与本轮无关、也未触碰对应模块，属既有问题：
  `test_moneyflow_source.py::test_normalizes_units_codes_and_source`（汇率/市场后缀）、
  `test_screener_query.py::test_query_builder_uses_derived_expression_and_codes`（参数顺序）。

---

## 5. 仍未关掉的口子

1. `index_weight` 是月频快照，月内成分变动按上月末处理；
2. `all_a` 的 ST 过滤用的是最新 `is_st`，不是 PIT；
3. **研究层 `Panel` 还没有 margin/moneyflow 列** → `factor_report.py` 无法复核这 5 个
   sentiment 因子，只能靠回测看效果；
4. 中证1000 成分缺 2020 以后数据，小盘结论目前只能算方向性；
5. 上述 2 个既有测试失败未修。
