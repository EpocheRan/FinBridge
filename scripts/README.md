# scripts 说明

> 结论先说：**没有任何业务模块 import `scripts/` 或 `tests/`**（全仓 grep 结果为 0），
> 因此这批脚本可以独立整理、归档或删除，不会影响 factors / research / backtest / screener。

## 在用（评估与报告主链路）

| 脚本 | 用途 |
|---|---|
| `factor_report.py` | 单因子深评报告（默认 profile、全市场） |
| `factor_eval.py` | 批量快筛（`--profile quick`） |
| `profile.py` | 评估 profile 管理：seed / list / show / save |
| `verify_equivalence.py` | 新旧实现 1e-9 等价性回放 |
| `factor_audit.py` / `factor_corr.py` / `factor_scorecard.py` | 历史产物：审计、相关矩阵、批量评分卡 |
| `export_pool.py` | 把研究池导出成回测可用的显式股票池 |

## 数据构建（按需执行，非日常）

`init_database.py`、`backfill_*.py`、`repair_index_data.py`、`calibrate_directions.py`、
`migrate_database.py`、`migrate_factors.py`。

## 一次性 / 历史（可归档，不建议再执行）

| 脚本 | 说明 |
|---|---|
| `verify_p0_p2.py` | 早期 P0–P2 验收脚手架，已被 `tests/` 与实际链路取代 |
| `migrate_market_type.py`、`migrate_market_to_exchange.py` | `market` 字段两次迁移，库内已完成；无文档引用 |
| `research_composite_demo.py`、`research_limitup_check.py`、`research_reversal_*.py` | 一次性研究脚本，结论已进文档 |

> 归档建议：把「一次性 / 历史」整批移到 `scripts/archive/`，保留可追溯性；
> 确认无人引用后再删除。当前先以本节标注，不做批量移动，避免破坏文档链接。
