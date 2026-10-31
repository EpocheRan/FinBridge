# 05 · research（因子研究报告与评分卡）

## 1. 架构与依赖

### 1.1 职责边界

`research` 回答一个问题：**这个因子值不值得用**。它把因子面板 + 行情，变成一份可评审的
报告（HTML）+ 一条可追踪的评分记录（`factor_scorecard`）。

两节核心内容对应两个维度：

- **时间维度**：IC 是否持续（均值/t 值）、跨周期是否稳定（分年/分状态）、衰减多快；
- **空间维度**：分层是否单调、多空有无经济意义、在市值/行业子样本中是否成立。

它**不**做选股、不做回测撮合；只用**可执行口径**（T+1 开盘、单腿往返 20bp、多空合计 40bp）评价。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | `factors` | `F.ts(...)` 取价量因子 |
| 上游 | `data_interface` | 只读 `common.config` / `timeutil` 建连接 |
| 上游 | `factor_value_daily` | 财务因子从物化表读 |
| 下游 | QuestDB `factor_scorecard` | 评级入库 |
| 下游 | `output/factor_reports/` | HTML 报告 + 汇总 CSV |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `report/metrics.py` | `build_report`：IC/分层/衰减/独立 t/中性化/净值/成本/容量/可成交性/结构化自检 |
| `report/diagnostics.py` | 断言（ρ↔价差、IC→价差传导、价差 vs 成本、方向先验）、失效归因、家族定义 |
| `report/definitions.py` | 因子可读定义（公式/单位/变换/方向/PIT）与增量性对照组 |
| `report/panel.py` | `load_panel`：新 schema 取数、后复权、行业/市值/退市、末日覆盖检查 |
| `report/db.py` | QuestDB REST 访问（唯一入口） |
| `report/rating.py` | `rate(report)`：100 分制评分卡 + 一票否决 + 四象限 |
| `report/render.py` | `render(report, score, meta)`：教科书式 HTML（内联 SVG） |
| `report/charts.py` | SVG 图表原语：折线/柱状（带误差棒）/净值+回撤/评分条/象限 |
| `report/runner.py` | `generate`（单因子端到端） |
| `report/scorecard.py` | `factor_scorecard` 建表与写入 |

### 1.4 数据流

```
load_panel(pool_date, size, start, end)         # 点位池 close/open/regime/market_cap/industry
      │
factor 面板（date × symbol）
      │
build_report(factor, close, open, ...)          # 4 个持有期 IC + 分层 + 衰减
      │
rate(report)                                    # 评分 + 否决 + 象限
      ├─ render → output/factor_reports/<id>.html
      └─ scorecard.save → factor_scorecard
```

## 2. 核心类与方法

### 2.1 类/函数表

| 名称 | 签名 | 说明 |
|---|---|---|
| `build_report` | `(factor, close, open_, *, factor_id, category, direction, direction_source="registry", universe, horizons=(1,5,10,20), primary_horizon=5, groups=5, cost_bps=20, regime, regime_alt=None, market_cap, industry, amount=None, benchmark=None, peers=None, definition=None, pool=None, freshness=None, exclude_limit_up=False, full=True)` | 生成 `FactorReport` |
| `FactorReport` | dataclass | 字段：`ic_stats`、`ic_half`、`ic_neutral`、`ic_by_regime`/`regime_stats`、`ls_nav`/`drawdown`/`perf`/`annual_returns`/`annual_ic_return`、`filter_test`、`spread_gross`/`spread_net`/`cost_mean`/`sigma_5d`、`transmission`、`diagnostics`、`attribution`、`tradeability`/`sell_side`/`cost_table`/`capacity`、`incrementality`/`pool_robustness`、`definition`/`pool`/`freshness` |
| `rate` | `(report) -> dict` | `grade/score/time_score/space_score/quadrant/vetoes/items/attribution/detail` |
| `render` | `(report, score, meta) -> str` | HTML 字符串 |
| `generate` | `(factor_id, *, pool_date, pool_size, start, end, out_dir, with_peers=True, robustness=True, run_id=None)` | 单因子端到端，写 HTML+JSON（同一 `run_id`） |
| `load_panel` | `(pool_date, pool_size, start, end, *, min_amount=None, max_amount=None, warmup_days=2000)` | 点位池行情面板；多取预热期，因子算完裁到 `eval_start` |
| `forward_returns` | `(close, open_, horizons)` | `T+1 开盘买、T+h 收盘卖` |
| `tradeable_mask` | `(close, open_)` | t+1 开盘一字涨停/停牌 → 不可买 |
| `newey_west_t` | `(series, lag)` | NW t 值（lag = h−1） |
| `scorecard.save` | `(report, *, run_id, category, horizon=5, score)` | 写评分卡 |

### 2.2 用法

```python
from research.report import metrics, rating, render, runner
from research.report.panel import price_factor_panel

panel = runner.load_panel("2017-11-01", 200, "2019-01-01", "2026-10-29")
factor = price_factor_panel("liquidity.amount_ma_20d", panel)

report = metrics.build_report(
    factor, panel.close, panel.open,
    factor_id="liquidity.amount_ma_20d", category="liquidity", direction=-1,
    universe="点位池 200", regime=panel.regime,
    market_cap=panel.market_cap, industry=panel.industry,
    amount=panel.amount, benchmark=panel.benchmark,
)
score = rating.rate(report)
html = render.render(report, score, {"run_id": "20261029T1600", "generated_at": "..."})
```

批量（推荐）：

```bash
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --full --render
python scripts/factor_scorecard.py --source financial --pool 200 --start 2019-01-01 --full --render
python scripts/factor_audit.py --dir output/factor_reports   # 全库断言 + 家族报告
python scripts/factor_corr.py --source price                 # 143×143 相关矩阵 + 聚类
python scripts/calibrate_directions.py                       # 方向校准（默认只报告）
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **重叠样本看 NW-t，保守判断看独立 t** | `ic_stats` 同时给 `t`（NW, lag=h−1）与 `t_indep/n_indep`（每 h 日取一个观测） |
| **中性化** | `ic_neutral` 为原始/市值/行业/双中性四行；残差 IC 仍是 Spearman |
| **增量性不是占位** | `incrementality` 由同类因子相关 + 剔除同类后的残差 IC 计算；`peers` 为空时才回退 8/15 |
| **稳定性看 CI，不看符号** | 分年 CI 显著率 40% + 分状态显著率 30% + 滚动 12 个月 IC 同号率 30%；时间维度按同权重合成 |
| **预测力是连续加权** | `30×(0.6×min(1,IC/0.05)+0.4×min(1,ICIR/0.5))`，IC/ICIR 为负记 0；不再用 `min()` 封顶 |
| **方向不翻转** | `sig = raw × direction`，direction 来自 registry/家族先验；IC 为负记「方向错」，不在同一样本内翻转（否则等于样本内择向） |
| **价差分毛/净** | 单调性 ρ 用毛收益（避免换手差异污染），可交易性用毛价差 CI vs 成本；净价差单列 |
| **窗口预热** | `load_panel` 从 `start−2000d` 取行情，因子算完裁到 `start`；否则 504d 因子缺前两年，变体之间不是同一评估区间 |
| **断言只在可证边界硬失败** | n=5 时 ρ≥0.8 才必然 Q5>Q1；0<ρ<0.8 可以出现 Q5<Q1（非单调排列），只报警 |
| **IC→价差传导** | 期望 = IC×σ_5d×2.80（五分位近似）；实测/期望 不在 (0,2) 报警 |
| **不可交易用区间** | 毛价差 95% 上界<成本→触发；下界>成本→通过；否则边界（不再用点估计±1.96SE 的死区） |
| **D 级必须归因** | `无信号(IC→0)` / `方向错` / `复现失败` / `数据不足` / `传导断裂`，由断言自动写入 |
| **过滤价值要实测** | `filter_test` 比较剔除 Q1 后的夏普/收益/尾部亏损；Q1 收益为正且改善不足时不得称「过滤」 |
| **样本池稳健性** | `generate(robustness=True)` 用 2019-01/2021-01 重取样池；`pool_robustness` 列 IC/独立 t/价差 |
| **选样内生性按池渲染** | 只有成交额族输出「用分子选池」段；其它价量因子输出「流动性倾斜池」；财务池输出覆盖度选样 + PIT，不再整段复制 |
| **选样漏斗** | `pool.funnel` 逐级给数量（候选→板块→有行情），财务池额外给「有财务数据→前300→入池」 |
| **PIT** | 财务字段 `as_of=available_at`、`lag` 交易日；重述只保留最近一次入库值，报告按「上限」读 |
| **run_id 同源** | HTML/JSON/评分卡同一 `run_id`；JSON 全量落盘（`build_payload`），审计不再解析 HTML |
| **家族报告** | `factor_audit.py` 输出 `families/*.html` 与 `library_report.html`；单因子 HTML 作附录，避免 143 份同构报告平铺 |
| **卖出端符号** | `_sell_side_summary` 必须传 `analysis_factor`（已对齐方向），否则 Q5−Q1 会算成 Q1−Q5 |
| **换手单位** | 报告同时给「每期 5 日」与「年化 ×242/5」；否决阈值 80% 指每期 |
| **成本按换手扣除** | `cost = turnover × cost_bps`；多空另计 `(turn_Q5+turn_Q1) × cost_bps`，不是每次无条件扣 20bp |
| **末日覆盖检查** | 最后一日标的数 < 前一日 80% 时整日回退，并在 `freshness.partial_last_day` 记录 |
| **regime 基准** | 用日线完整的创业板指 399006；000852 在 2019–2021 缺日，会把年份效应伪装成状态效应 |
| **方向自适应会翻转因子** | 已移除。旧版发现设计方向 IC 为负就翻，导致 4/4 强因子 IC>0、ρ 与价差打架；现在方向事先给定，负 IC 记「方向错」 |
| **单调性必须用每期均值** | 复利净值会被波动拖累，制造假单调。`quantile_returns` 存的是**每期收益均值**，净值只作展示 |
| **IC 已向量化** | 逐日循环 1.58s → 整表 rank 0.05s（35×），数值差 1e-16；不要再写回循环 |
| **可成交过滤默认关闭** | `exclude_limit_up=True` 会剔除 t+1 开盘一字涨停；实测大盘池影响 0.1~0.5%，小盘池 0.2~0.8% |
| **否决规则优先级** | 证据不足（`|IC|<0.015` 或显著年份不足/方向不一致）、方向与先验相反、覆盖率<70%、单调性<0、不可交易（区间判定）、每期换手>80% —— 任一触发即封顶 C |
| **`primary_horizon` 不是属性** | 它是 `build_report` 的参数，`FactorReport` 里只有 `horizons`；评分卡用固定 `horizon=5` |
| **报告目录固定** | 只写 `output/factor_reports/`，不做预览截图，不产出中间图（见 `output/README.md`） |
| **评分卡去重键** | `DEDUP UPSERT KEYS(ts, run_id, factor_id)`：同一 run 重跑会覆盖，不同 run 保留历史 |
| **样本量决定可信度** | 财务因子按覆盖度选样（2019 起约 88 只 × 7.8 年），IC 的统计功效仍弱于价量因子，D 级不等于"因子坏" |
