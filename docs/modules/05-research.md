# 05 · research（因子研究报告与评分卡）

## 1. 架构与依赖

### 1.1 职责边界

`research` 回答一个问题：**这个因子值不值得用**。它把因子面板 + 行情，变成一份可评审的
报告（HTML）+ 一条可追踪的评分记录（`factor_scorecard`）。

两节核心内容对应两个维度：

- **时间维度**：IC 是否持续（均值/t 值）、跨周期是否稳定（分年/分状态）、衰减多快；
- **空间维度**：分层是否单调、多空有无经济意义、在市值/行业子样本中是否成立。

它**不**做选股、不做回测撮合；只用**可执行口径**（T+1 开盘、双边 20bp）评价。

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
| `report/metrics.py` | `build_report`：IC/分层/衰减/换手/分状态/可成交性 |
| `report/rating.py` | `rate(report)`：100 分制评分卡 + 一票否决 + 四象限 |
| `report/render.py` | `render(report, score, meta)`：教科书式 HTML（内联 SVG） |
| `report/charts.py` | SVG 图表原语：折线/柱状/热力/评分条/象限/直方图 |
| `report/runner.py` | `load_panel` / `compute_factor` / `generate`（单因子端到端） |
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
| `build_report` | `(factor, close, open_, *, factor_id, category, direction, universe, horizons=(1,5,10,20), primary_horizon=5, groups=5, cost_bps=20, regime, market_cap, industry, exclude_limit_up=False)` | 生成 `FactorReport` |
| `FactorReport` | dataclass | 字段：`ic` / `ic_stats` / `decay` / `quantile_returns` / `monotonicity` / `turnover` / `long_short` / `limit_up` / `warnings` |
| `rate` | `(report) -> dict` | `grade/score/time_score/space_score/quadrant/vetoes/items` |
| `render` | `(report, score, meta) -> str` | HTML 字符串 |
| `generate` | `(factor_id, *, pool_date, pool_size, start, end, out_dir)` | 单因子端到端，写 HTML+JSON |
| `load_panel` | `(pool_date, pool_size, start, end, *, min_amount=None, max_amount=None)` | 点位池行情面板 |
| `forward_returns` | `(close, open_, horizons)` | `T+1 开盘买、T+h 收盘卖` |
| `tradeable_mask` | `(close, open_)` | t+1 开盘一字涨停/停牌 → 不可买 |
| `newey_west_t` | `(series, lag)` | NW t 值（lag = h−1） |
| `scorecard.save` | `(report, *, run_id, category, horizon=5, score)` | 写评分卡 |

### 2.2 用法

```python
from research.report import metrics, rating, render, runner

panel = runner.load_panel("2017-11-01", 200, "2019-01-01", "2026-10-28")
factor = runner._factor_panel(...)   # 或自算 date×symbol

report = metrics.build_report(
    factor, panel.close, panel.open,
    factor_id="momentum.ret_21d", category="momentum", direction=1,
    universe="点位池 200", regime=panel.regime,
    market_cap=panel.market_cap, industry=panel.industry,
)
score = rating.rate(report)
html = render.render(report, score, {"run_id": "20261029T1600", "generated_at": "..."})
```

批量（推荐）：

```bash
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --render
python scripts/factor_scorecard.py --source financial --pool 200 --start 2024-01-01 --render
python scripts/calibrate_directions.py            # 方向校准（默认只报告）
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **方向自适应会翻转因子** | `build_report` 发现设计方向 IC 为负时会自动翻，并在 `warnings` 写明；若经济逻辑不支持反向，应判无效而不是采纳 |
| **单调性必须用每期均值** | 复利净值会被波动拖累，制造假单调。`quantile_returns` 存的是**每期收益均值**，净值只作展示 |
| **IC 已向量化** | 逐日循环 1.58s → 整表 rank 0.05s（35×），数值差 1e-16；不要再写回循环 |
| **可成交过滤默认关闭** | `exclude_limit_up=True` 会剔除 t+1 开盘一字涨停；实测大盘池影响 0.1~0.5%，小盘池 0.2~0.8% |
| **否决规则优先级** | `|IC|<0.02`、覆盖率<70%、年同号<60%、单调性<0、多空价差<0.2%、换手>80% —— 任一触发即封顶 C |
| **`primary_horizon` 不是属性** | 它是 `build_report` 的参数，`FactorReport` 里只有 `horizons`；评分卡用固定 `horizon=5` |
| **报告目录固定** | 只写 `output/factor_reports/`，不做预览截图，不产出中间图（见 `output/README.md`） |
| **评分卡去重键** | `DEDUP UPSERT KEYS(ts, run_id, factor_id)`：同一 run 重跑会覆盖，不同 run 保留历史 |
| **样本量决定可信度** | 财务因子只有 100+ 只 × 2 年，IC 的统计功效远弱于价量因子，D 级不等于"因子坏" |
