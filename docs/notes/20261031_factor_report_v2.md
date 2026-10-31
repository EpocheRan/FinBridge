# 2026-10-31 因子报告 v2 加固备注

## 背景

针对 27 条因子报告审计意见，修的是「报告结论能否被复核」，不是调分数线：

- 方向：`sig = raw × direction` 唯一入口，删除样本内按 IC 符号翻转；
- 窗口：`load_panel` 多取约 5.5 年预热，504d 因子不再缺前两年；
- 价差：期均值、毛/净分列；多空净价差 = 毛价差 − 两条腿换手成本；
- 成本：NAV/夏普/回撤/净价差 t 全部走同一条净价差；
- 断言：ρ↔价差、IC→价差传导、价差 vs 成本、方向先验、同输入复现；
- 归因：D 级强制输出 `无信号 / 方向错 / 复现失败 / 数据不足 / 传导断裂`；
- 选样：按 pool config 条件渲染，财务池补 PIT、漏斗、有效起点；
- 产物：HTML/JSON 同 `run_id`，新增 `factor_audit.py`、`factor_corr.py`。

完整 27 项对照见 `docs/design/factor_report_v2.md`。

## 全库实测（146 个因子 / 34 个家族）

| 指标 | 结果 |
|---|---|
| 等级分布 | 0A / 5B / 18C / 123D（旧 0A/2B/5C/136D） |
| 断言 | pass 3 / boundary 56 / warn 77 / fail 10 |
| fail 构成 | 7 不可交易（负价差）、3 方向与先验相反 |
| D 级归因 | 无信号 38 / 传导断裂 68 / 证据不足 12 / 数据不足 2 / 方向错 3 |
| 同变体问题 | 2 个（turnover_bias 价差 0.03–0.11%；risk.ivol csi300/sh 价差符号相反） |
| 相关矩阵 | 81×81、71 对 \|ρ\|≥0.9、16 簇；20d vs 21d ρ=0.9995 |
| 复现 | HTML/JSON `run_id` 不一致 0 份 |

关键纠错：

- `value.fcf_p`：旧 IC 0.001 → +23.0%/Sharpe 1.17；新 D 16.7，净价差 −0.136%/期、年化 −8.0%。
- `value.ep`：旧 −8.0%/Sharpe −0.31；新 C 58.2，毛价差 −0.038%，标「分层反向」。
- `momentum.rsrs`：毛价差 +0.137%，扣 20bp 后净 −0.062%、年化 −3.9%；旧 −5.6% 是成本+几何复利，不是方向恒等式错误。
- `turnover_std_bias_42_252`：B 81.5，净 +0.172%/期、年化 +7.8%、Sharpe 0.65。

## 复现

```bash
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --full --render
python scripts/factor_scorecard.py --source financial --pool 200 --start 2019-01-01 --full --render
python scripts/factor_audit.py --dir output/factor_reports
python scripts/factor_corr.py --source price --sample-dates 24
python -m pytest tests/test_research_report.py -q
```

产物在 `output/factor_reports/`：单因子 HTML/JSON、`audit/library_report.html`、
`audit/audit_*.md`、`audit/corr_report.html`、`audit/families/*.html`。

## 已知边界

- 仍是固定基准日池，不是 point-in-time 全 A；未补 132 只无历史行情的退市股。
- 财务重述未逐期回溯；PIT 只到「公告日 + lag」。
- 等级线未按族内分位重标（当前 0A/5B，未达 A 5–10%、B 15–20% 目标）；
  按审计意见，先修断言再定标。
- `liquidity.vol_alpha_300d_csi300`、`risk.vol_beta_120d_csi300` 因数据缺失，
  报告中归因为「数据不足」，不是因子无效。

## 本次提交范围

只提交因子报告相关代码/文档/测试；工作区里 acquisition/sentiment/analysis 等在途改动
未纳入本次提交。
