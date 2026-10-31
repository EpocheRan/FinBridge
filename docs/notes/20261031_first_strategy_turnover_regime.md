# 第一条策略基线：换手波动状态（2026-10-31）

> 目的：把「框架和数据都做完了」变成「有一条可复现净值」。
> 结论：用库内冠军因子 `liquidity.turnover_std_bias_42_252` 跑通横截面策略，
> 2020-01 ~ 2026-10 年化 **8.23%**、Sharpe **0.54**、最大回撤 **-23.2%**；
> 参数不敏感，说明赚的是因子的钱，不是调参的钱。

---

## 1. 基线结果

口径：显式池 200 只（2017-11-01 成交额前 200，剔除北交所）、月度调仓、
T+1 开盘成交、双边 20bp、初始 100 万。

| 变体 | 年化 | Sharpe | 最大回撤 | 年化波动 |
|---|---|---|---|---|
| **top30 等权（基线）** | **8.23%** | **0.537** | **-23.2%** | 17.6% |
| top20 等权 | 8.14% | 0.525 | -22.1% | 18.0% |
| top50 等权 | 7.11% | 0.490 | -23.7% | 17.0% |
| top30 逆波动加权 | 6.87% | 0.483 | -22.3% | 16.6% |

读法：

- 四个变体 Sharpe 都在 0.48–0.54，**没有一个是调出来的尖峰**，这是好因子的特征；
- 逆波动加权在这个因子上**反而更差**（6.87% → 0.483）：因子本身已经在筛低拥挤标的，
  再按波动率倒数加权等于重复惩罚低波动，收益被削掉；
- 月度调仓、top30、等权就是合理默认值，不必再搜。

复现：

```bash
python scripts/export_pool.py --pool-date 2017-11-01 --size 200 \
    --out strategies/pools/pool_2017_200.py          # 只需一次
python -m backtest run strategies/models/turnover_regime.py \
    --start 2020-01-01 --end 2026-10-29 --out console
# 换参数
python -m backtest run strategies/models/turnover_regime.py \
    --start 2020-01-01 --end 2026-10-29 --param top_n=20 --param weighting=inverse_vol
```

---

## 2. 跑通过程中发现的三个框架事实（会决定你后面能做什么）

### 2.1 回测层只支持 explicit universe —— 已绕过

`backtest/application/runner.py::_resolve_universe` 对 `all_a` / `index` 直接抛
`ConfigError`（"MVP 只支持 explicit universe"）。所以策略必须写死代码列表。

新增 `scripts/export_pool.py`：把研究层的选池逻辑（`load_panel`）导出成
`strategies/pools/pool_2017_200.py` 里的常量，策略直接 import。
**代价**：池子固定 = 幸存者偏差 + 风格漂移，8.23% 要按此折价。
**解法**：把 `index_weight` 的 PIT 成分池接进回测层（约 1 人日），这是后面所有
截面策略的前提。

### 2.2 回测数据源原本不取 `turnover` —— 已修

`backtest/infrastructure/data/questdb.py` 的 SELECT 只有
`open, high, low, close, volume, amount, adj_factor`，没有 `turnover`。
换手类因子在 `_turnover()` 里退化为 `volume / float_share`，而 `float_share`
也不在帧里 → **整列 NaN → 调仓计划为空 → 报错「没有可执行的调仓目标」**。

这是最容易踩的坑：**因子在研究报告里是 B 级，在回测里却一条信号都没有**。
已在 SELECT 里补 `turnover`（分钟线表没有该列，按表名分支处理，缺失时补 NaN 列），
顺手确认 `ohlcv_1w` 也有该列。

### 2.3 回测面板只有 OHLCV，没有 margin / moneyflow —— 未接

`build_panel` 的输入帧来自 `data_source.load`，只含行情列。上一轮验证过的两融因子
（`margin.balance_chg_20d`、`margin.short_ratio`，与冠军 ρ≈0.14/0.16，合成后 IC
+22%、净价差翻倍）**目前无法进入回测**。要接需要：

1. `backtest/infrastructure/data/questdb.py` 增加按 `(ts, code)` left join 两融/资金流；
2. `factors/panel.py` 的 `build_panel` 允许把这些列透传给算子；
3. 策略侧用 `F.composite([...])` 或自定义 op 合成。

估计 2–3 人日，是「L2 数据套利」第一次真正兑现的地方。

---

## 3. 四层框架 × 你的实际状态

| 层 | 做法 | 你的真实状态 | 判断 |
|---|---|---|---|
| **L1 公式套利** | 复现公开论文因子 | 146 个已评级，81 个进相关矩阵，**71 对 \|ρ\|≥0.9 / 16 簇** | **已饱和**，再做就是第 12 个变体 |
| **L2 数据套利** | 用别人没用的数据造因子 | 8 张结构表约 1,200 万行，**0 个因子**；两融已实测出 2 个正交候选 | **唯一高 ROI，立即可做** |
| **L3 结构套利** | 同数据换切法：行业内、条件化、事件窗 | 冠军做行业内 z 后 IC 0.0337↓ 但净价差 0.65%↑（已验证）；`regime` 字段已算出来但**没有任何因子/策略用它**；事件型表 `announcement` 只有 2024-11 起 | **免费但没用**；行业/regime 立即可做，事件窗受数据长度限制 |
| **L4 微观结构** | 可交易性本身当信号 | `ohlcv_5m`：**2 只股票 × 24 个交易日**；`ohlcv_15m`：**0 行**；`intraday_baseline`：空 | **基本没有**，不是「已有部分」 |

> 关于 L4 的更正：那张图写「5min 数据（已有部分）」，实测是 2 只 × 1 个月，
> 做不了任何截面微观结构因子。它现在是**采购/接入问题**，不是想法问题——
> 在拿到全市场分钟线之前，L4 不应进入排期。

---

## 4. 接下来三条分支（按 ROI 排序，每条给最小可交付）

### A. L2 兑现：两融因子接入回测（首选，2–3 人日）

- 最小可交付：`questdb.py` join `margin_detail` → 策略 `turnover_regime_margin.py`
  用 `rank(冠军) + rank(两融)` 合成 → 对比基线 Sharpe；
- 预期：IC 0.0449 → 0.0546（+22%），净价差 +0.48% → +1.00%/期；
- 验收：同一区间 Sharpe 提升 ≥ 0.15，且两融因子单独 IC > 0.02。

### B. L3 免费增量：行业内中性 + regime 条件（1–2 人日）

- 行业内：`rank` 里对因子做行业 demean 再排序（`panel` 需暴露 industry）；
- regime：`factors/panel.py` 已能算 bull/bear/neutral，加一条「bear 时空仓或减半」；
- 预期：IC 会降（0.0449→0.0337）但净价差升（0.48%→0.65%），且回撤收敛；
- 验收：Sharpe 提升或最大回撤改善 ≥ 3pct。

### C. L1 残值：救活 68 个「传导断裂」因子（1 人日先做体检）

- 这些因子有 IC 无价差，问题通常在极值/分位内不可交易/成本；
- 最小可交付：给 `factor_report.py` 加 `--cost` 与 `--delay-exit` 扫描，
  看哪几个在 10bp 或延迟一天退出的假设下价差转正；
- 验收：救回 3–5 个 C 级以上的可交易因子。

---

## 5. 边界

- 样本池固定 2017-11-01，**不是 PIT 成分**，退市股缺失带来向上偏差；
- 2020 起算（252 日预热），未覆盖 2018 单边熊；
- 未计冲击成本与容量约束（`_capacity` 只在报告里有，回测走固定 20bp）；
- 单一因子单条策略，未做多因子风险平价与行业约束。
