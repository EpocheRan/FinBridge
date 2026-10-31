# 因子研究报告 v2 加固说明（27 项审计的落点）

> 对应 2026-10-30 的 27 条审计意见。目标不是「让评级好看」，而是让报告里的每个结论
> 都能被程序复核：方向、价差、成本、样本池、PIT、归因各自只有一个入口。

## 1. 结构性修复

### 1.1 方向：唯一入口，禁止样本内翻转

旧版 `build_report` 发现设计方向 IC 为负就 `analysis_factor = -analysis_factor`。
这等价于用全样本 IC 符号择向：随机因子的 IC 会被系统性抬成正数（审计看到的
「4/4 强因子 IC>0」），同时把 Q1/Q5 与 ρ 的符号关系搅乱。

现在：

```python
sig = factor * direction   # direction ∈ {+1, -1}
```

- `direction` 只来自 `definitions.describe`（registry 先验 / 家族先验 / 人工指定）；
- IC、分组、净值、换手、卖出端全部从 `sig` 派生；
- IC 为负 → 记 `方向错`，触发「方向与先验相反」否决，**不翻转**；
- 方向来源写入报告与 JSON（`direction_source`）。

### 1.2 价差：期均值，毛/净分开

- `quantile_returns_gross` / `quantile_returns_net` 并列；
- 单调性 ρ 用**毛收益**（各组换手差异会污染净收益）；
- 可交易性用**毛价差 CI vs 单期成本**；
- 年化只作展示，不进入任何断言。

### 1.3 断言：只在可证边界硬失败

`research/report/diagnostics.py` 提供四条断言，写入 `report.diagnostics` 与 JSON：

| 断言 | 通过条件 | 失败/提示 |
|---|---|---|
| `rho_spread` | ρ≥0.8 时 Q5>Q1；ρ≤−0.8 时 Q5<Q1 | 中间区间只 warn |
| `ic_transmission` | 实测毛价差 / (IC×σ_5d×2.80) ∈ (0,2) | 符号相反或 >2× 报警 |
| `tradeability` | 毛价差 95% 下界 > 成本；或上界 < 成本 | 区间跨成本 → 边界 |
| `direction` | IC 与设计方向同号 | IC<−0.01 且 t<−2 → fail |
| `reproduction` | 同输入两次逐点相等 | 单因子 `generate` 默认校验 |

### 1.4 否决：区间判定，不再有死区

旧规则「|价差−成本| < 1.96SE 判边界」在成本 0.20% 时制造了约 0.58pp 的死区，
既不说通过也不说触发。现在：

- 毛价差 95% **上界 < 成本** → 触发「不可交易」；
- **下界 > 成本** → 通过；
- 否则 → 边界（明确写「区间跨成本」，不算通过）。

### 1.5 失效归因：D 级必须说明为什么

`无信号(IC→0)` / `方向错` / `复现失败` / `数据不足` / `传导断裂`。
归因由断言自动写入，报告 04 节与 JSON `attribution` 同时可见。

### 1.6 窗口预热：504d 因子不再少两年样本

`load_panel` 原来只从评估起点取数，504 日窗口的因子前两年全是 NaN，等于
「42_252 评估 2020–2026、21_504 评估 2021–2026」，同族价差自然差 4 倍。
现在：

- `load_panel(..., warmup_days=2000)` 多取约 5.5 年行情；
- `price_factor_panel` 用预热数据算因子，再裁到 `eval_start`；
- `build_report` 把 close/open 裁到因子日期，成本/可成交/容量分母不再混入预热期。

修复后价量因子在 2019 起覆盖率约 97%，504d 与 20d 变体是同一评估区间。

### 1.7 多空成本：两条腿都要付

旧版用 `Q5_net − Q1_net` 当多空净价差：两条腿成本相同时会互相抵消，等于漏算空头腿。
现在统一：

```
毛价差 = Q5_gross − Q1_gross
成本   = (turn_Q5 + turn_Q1) × 20bp
净价差 = 毛价差 − 成本
```

NAV、夏普、回撤、净价差 t 全部用这条净价差；分层表的单组净收益另算。

## 2. 27 项对照

| # | 结论 | 落点 |
|---|---|---|
| 1 | **部分采纳**：ρ 与价差用期均值校验；但 `ρ>0 ⇒ Q5>Q1` 不成立，硬边界改为 |ρ|≥0.8 | `diagnostics.rho_spread_identity` |
| 2 | 采纳：唯一入口 `sig=raw×direction`，删除样本内翻转 | `metrics.build_report` |
| 3 | 采纳：窗口预热统一评估区间 + 复现断言 + 同族量级检查 | `panel.load_panel(warmup_days)`、`runner.verify_repro`、`factor_audit.family_consistency` |
| 4 | 采纳：IC→价差传导断言（系数用 2.80，见 §3） | `diagnostics.ic_spread_transmission` |
| 5 | 采纳：区间判定；不再用点估计±1.96SE 的死区 | `diagnostics.tradeability_interval`、`rating` |
| 6 | 采纳：全库跑断言并输出违规清单 | `scripts/factor_audit.py` |
| 7 | 采纳：D 级失效归因标签 | `diagnostics.attribute_failure` |
| 8 | 采纳：断言全部用期均值；年化仅展示 | `metrics` / `rating` |
| 9 | 采纳：报告顶部「局部评级，禁止跨族比较」；家族汇总 | `render`、`factor_audit` |
| 10 | 部分采纳：财务池可 2019 起；<5 年窗口显著年门槛降为 2 | `rating.rate`、`factor_scorecard --start` |
| 11 | 采纳：PIT 写入定义与报告（as_of=available_at、lag、重述说明） | `definitions` / `render` |
| 12 | 部分采纳：披露固定基准日池非 PIT 全 A；未改选池算法 | `render.pool_narrative`、重估条件 |
| 13 | 部分采纳：披露退市缺失数量与方向；未补退市行情 | `panel.pool`、报告局限 |
| 14 | 采纳：选样段按 pool config 条件渲染（成交额族/其它价量/财务） | `render` |
| 15 | 采纳：表内字段与表注同源；财务池单独口径 | `factor_scorecard` |
| 16 | 采纳：选样漏斗逐级给数量 | `panel.pool["funnel"]`、`render` |
| 17 | 部分采纳：输出族内分位与分布目标；不强行改等级线 | `factor_audit` |
| 18 | 采纳：预测力改连续加权，去掉 min() 封顶 | `rating` |
| 19 | 采纳：稳定性加入滚动 12m IC 同号率（40/30/30） | `rating` |
| 20 | 采纳：预测力+稳定性合为一维 45，总权重 45/15/20/20 | `rating` |
| 21 | 采纳：价量 81×81 全矩阵、24×24 家族矩阵、|ρ|≥0.9 聚类（71 对；20d/21d=0.9995） | `scripts/factor_corr.py` |
| 22 | 采纳：34 份家族报告 + library 汇总，单因子 HTML 作附录 | `factor_audit` |
| 23 | 采纳：HTML/JSON/评分卡同一 run_id；JSON 全量落盘 | `runner.build_payload`、`factor_scorecard` |
| 24 | 采纳：残差为负、原始 IC 为负、或相关≥0.95 时增量性 0 分；不再输出 −817% / +270% | `metrics._incremental`、`diagnostics.retention_note` |
| 25 | 采纳：方向措辞统一为「原值不取负，评估时 ×direction」 | `definitions._direction_text`、`render` |
| 26 | 采纳：表注按 family 渲染（size 不再同义反复） | `render` |
| 27 | 采纳：每份报告加 3 条重估条件 | `render` |

## 3. 与原始建议的三处差异（必须说明）

### 3.1 `ρ>0 ⇒ Q5>Q1` 不是恒等式

n=5 时 ρ=1−Σd²/20 没错，但「Q5<Q1 要求 Σd²≥20」不成立。反例：组均值排序
`[3,1,4,5,2]`（π 为名次），Σd²=16，ρ=+0.2，而 Q5（第 2 名）< Q1（第 3 名）。
因此：

- ρ≥0.8 时，Σd²≤4，只允许一次相邻交换，Q5>Q1 **必然**成立 → 可硬断言；
- 0<ρ<0.8 时存在合法的非单调排列 → 只报警，不判失败。

若强行 `if rho>0: assert spread>0`，会把非单调但真实的因子误杀。

### 3.2 IC→价差系数：1.76 是单侧尾部，不是 Q5−Q1

正态下 E[Z|top 20%]=1.395，Q5−Q1≈2.79；1.76 更接近单侧 10% 尾部均值。
报告改用 **2.80**，并允许 `QUINTILE_SPREAD_MULTIPLIER` 覆盖。用 1.76 会把期望价差
低估约 37%，制造大量假「传导断裂」。

### 3.3 rsrs 仍是「边界」不是「触发」

rsrs 毛价差 0.10%、成本 0.20%，但 95% CI 上界约 0.30% > 成本。按区间规则只能判边界：
统计上无法排除它能覆盖成本。若用点估计判「触发」，会与「区间判定」自相矛盾。
真正被新规则抓到的是**负价差且上界低于成本**的因子（例如毛价差 −0.23%、上界 −0.04%）。

### 3.4 「IC 正、Q5−Q1 负」不一定是算错

`liquidity.turnover_ma_20d` 的 IC 约 +0.027，但 Q5−Q1 约 −0.24%/期。逐期检查：
中位数价差 +0.29%，均值被 Q5 的极端负收益拖成负数。也就是说，因子在**中位**有效，
但最高信号组有负尾风险。报告用 `ic_transmission` 报警并提示「不能只用 IC 判断好坏」，
而不是把它当计算错误；这正是 #4 想避免的「IC 与价差各自说话」。

## 4. 实测（2026-10-31 全库重跑）

- 146 个因子 / 34 个家族：**0A / 5B / 18C / 123D**（旧版 0A/2B/5C/136D）；
- 断言：pass 3 / boundary 56 / warn 77 / fail 10；fail 全部是「不可交易」或「方向与先验相反」，
  没有 ρ 恒等式失败；
- D 级归因：无信号 38 / 证据不足 12 / 传导断裂 68 / 数据不足 2 / 方向错 3；
- 同变体问题 2 个：`liquidity.turnover_bias` 价差 0.03–0.11%（同 IC≈0.025）、
  `risk.ivol_1320d` 的 csi300 与 sh 版价差 0.08% vs −0.02%；
- 价量相关矩阵 81×81、24 个家族、71 对 |ρ|≥0.9、16 个簇；`turnover_ma_20d` 与 `21d` 的 ρ=0.9995；
- 窗口预热后 504d 因子的样本从约 1230 日恢复到 1896 日，同一 `std_bias` 家族价差
  0.194%–0.235%（1.2×，旧版 4.3×）；
- HTML/JSON run_id 不一致 0 份。

## 5. 运行与产物

```bash
# 价量 / 财务（分开跑，避免 200 只价量池覆盖财务池）
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --full --render
python scripts/factor_scorecard.py --source financial --pool 200 --start 2019-01-01 --full --render

# 全库断言 + D 级归因 + 家族报告
python scripts/factor_audit.py --dir output/factor_reports

# 相关矩阵 / 聚类
python scripts/factor_corr.py --source price --sample-dates 24
```

产物：

```
output/factor_reports/<factor>.html / .json     # 同一 run_id
output/factor_reports/audit/audit_*.md/.csv     # 违规清单
output/factor_reports/audit/library_report.html # 家族汇总
output/factor_reports/audit/families/*.html     # 20+ 份家族报告
output/factor_reports/audit/corr_report.html    # 家族热力图
output/factor_reports/audit/clusters.json       # |ρ|≥0.9 的因子簇
```

## 6. 仍未完成（数据/研究决策，不是代码问题）

- **PIT 全 A 池**：当前仍是固定基准日池；需按日重建全 A（含退市）再重跑。
- **退市股行情**：132 只无历史行情，无法计算偏差上界；报告只披露缺失数量与方向。
- **等级线校准**：先修断言，再按族内分位定 A/B 目标；当前只输出分位，不强行改线。
- **财务重述回溯**：只保留最近一次入库值；严格 PIT 需要按公告日重放历史版本。
