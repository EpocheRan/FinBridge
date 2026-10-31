# analysis/ —— 一次性专题分析的工作区

这里放**脚本**，不放数据、不放产物。

## 这目录是干什么的

2026-10-30 两件事留下的可复现脚本：

1. **个股概率矩阵**（新乡化纤 000949 / 江淮汽车 600418 的 2/5/14 日走势）
2. **数据库巡检与修复**（schema 迁移、覆盖率审计、修复验收）

## 脚本清单

### 个股概率分析

| 脚本 | 作用 |
|---|---|
| `fetch.py` | 取数：两只票全历史 + 1400 只 A 股面板 + 资金流/两融/龙虎榜/估值 → `data/` |
| `model.py` | 12 维特征 + 2/5/14 日前瞻收益，特征近邻（k-NN）条件分布 |
| `archetype.py` | 「同类事件」筛选与基准率（含弱市子样本、分年度稳定性） |
| `low52.py` | 52 周低点附近的基准率（江淮这类破位票的补充口径） |
| `final.py` | 三口径加权、路径概率、分档矩阵、作图 |
| `earnings_event.py` | 财报披露日次日反应的实证（江淮 22 次） |

### 数据库巡检与修复

| 脚本 | 作用 |
|---|---|
| `db_health.py` | 运行健康：任务状态、新鲜度、字段质量、空表、WAL |
| `db_coverage.py` | 面板型表「应有 vs 实有」覆盖率（按年 / 按报告期） |
| `db_coverage2.py` | 个股口径覆盖率、2018 逐月、代码级缺口 |
| `coverage_after.py` | 修复后的覆盖率复核（按 code+market 分组） |
| `verify_repair.py` | 修复验收 9 项（type/market 合法性、重复、覆盖、任务） |

### 因子重建（内存受限时用）

| 脚本 | 作用 |
|---|---|
| `build_factors_batched.py` | 按 300~400 只分批重建 61 个质量/价值因子（宿主机一次性构建会 OOM） |
| `build_fund_factors.py` | 场内基金（ETF/LOF）22 个价格类因子重建 |

## 产物去哪了

按项目约定，**可交付产物在 `output/research/`**：

- `stock_probability_20261030.md` + `stock_matrix_000949_20261030.png` / `stock_matrix_600418_20261030.png`
- `db_repair_20261030.md`（schema 迁移 + 数据修复记录）
- `db_health_20261030.md`（覆盖率审计）

`analysis/data/`（几百 MB 的中间 CSV/PKL）与 `analysis/out/`（中间 JSON、日志、图表缓存）
属于**可再生成**的中间物，已清理，跑脚本时会自动重建。

## 复跑

```bash
# 个股概率矩阵（约 3 分钟，会写 analysis/data 与 analysis/out）
python analysis/fetch.py && python analysis/model.py && python analysis/archetype.py
python analysis/low52.py && python analysis/final.py

# 数据库巡检
python analysis/db_health.py
python analysis/db_coverage.py
python analysis/verify_repair.py
```

> 依赖本机 `.venv`（pandas / numpy / matplotlib）与运行中的 QuestDB（127.0.0.1:9000）。
