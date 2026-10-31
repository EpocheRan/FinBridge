# output 目录约定

> 运行时产物目录，**整个 `output/` 已在 `.gitignore` 中忽略**，不进版本库。
> 规则只有一条：**只放可交付产物，不放脚本、临时文件、半成品图**。

```
output/
├── factor_reports/     # 因子研究报告（正式产物）
│   ├── <factor_id>.html          单因子报告（内联 SVG，可打印）
│   ├── <factor_id>.json          同报告的元数据/评级
│   └── scorecard_<run_id>.csv    本批次全部因子的评级汇总
├── research/           # 研究结论与运行日志
│   ├── *.log                     长任务日志（回填/评分卡）
│   └── *.csv                     研究结论表（如反转密度阈值）
└── backtest/           # 回测引擎产物（按 run_id 分目录）
    └── <run_id>/{orders,trades,equity,metrics,...}
```

## 规则

1. **一个因子一份报告**：`factor_reports/<factor_id>.html`，重跑覆盖，不产生 `_v2`、`_final`。
2. **不落中间图**：预览截图、调试柱状图一律不写进 `output/`（写到系统临时目录或直接不落盘）。
3. **日志归 research**：跑批日志与结果表放 `research/`，文件名带 `run_id` 或日期。
4. **回测产物按 run_id 分目录**：由回测引擎自动管理，不手工搬动。

## 生成命令

```bash
# 单因子报告
python scripts/factor_report.py --factor momentum.ret@20 --pool 500

# 全量评分卡（写 factor_scorecard 表 + 汇总 CSV）
python scripts/factor_scorecard.py --source price --pool 200 --render

# 财务因子物化（STORED）
python scripts/backfill_factors.py --codes 120 --start 2024-01-01

# 方向校准（默认只报告）
python scripts/calibrate_directions.py
```
