# 10 · scripts（运维与研究脚本）

## 1. 架构与依赖

### 1.1 职责边界

`scripts/` 是**一次性/周期性的命令行工具集合**，不构成运行时依赖。每支脚本都是独立入口，
通过 `sys.path` 注入项目根目录，直接调用业务模块。

分四类：

| 类别 | 用途 | 是否可重复执行 |
|---|---|---|
| **回填（backfill_*）** | 补历史数据、补因子 | 幂等（按缺失判断） |
| **研究（research_*）** | 验证假设、跑实验 | 幂等（只读 + 输出表） |
| **报告（factor_*）** | 生成因子报告、评分卡 | 幂等（覆盖写） |
| **运维（init/migrate/repair/verify）** | 建库、迁移、修复、校验 | ⚠️ 需谨慎 |

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 下游 | `acquisition` / `data_interface` | 回填写库 |
| 下游 | `factor_engine` / `research` | 因子物化、报告、评分卡 |
| 下游 | `screener` / `backtest` | 校验、构建 |
| 输出 | `output/` | 严格遵守 `output/README.md` 结构 |

### 1.3 脚本清单

| 脚本 | 作用 | 典型用法 |
|---|---|---|
| `init_database.py` | 建全部表 | `python scripts/init_database.py` |
| `migrate_database.py` | 表结构迁移 | `python scripts/migrate_database.py` |
| `migrate_factors.py` | 因子注册表迁移 | `python scripts/migrate_factors.py` |
| `repair_index_data.py` | 修复指数日线（缺失/错标） | `python scripts/repair_index_data.py` |
| `verify_p0_p2.py` | 校验分时/板块/披露三条链路 | `python scripts/verify_p0_p2.py` |
| `backfill_daily_by_code.py` | 按代码补日线（含历史） | `--codes 600519 --start 2017-01-01` |
| `backfill_daily_pre2022.py` | 按日期补 2022 前全市场日线 | `--start 2017-11-01 --end 2022-10-01` |
| `backfill_intraday.py` | 补分时（新浪源） | `--codes 000949 --freq 5 --start 2026-10-01` |
| `backfill_boards.py` / `backfill_concept.py` | 板块目录/成分/日线 | `python scripts/backfill_boards.py` |
| `backfill_disclosure.py` | 披露日历与公告 | `python scripts/backfill_disclosure.py` |
| `backfill_moneyflow.py` | 资金流 | `python scripts/backfill_moneyflow.py` |
| `backfill_factors.py` | **财务因子物化**（STORED） | `--codes 120 --start 2024-01-01 --chunk 4` |
| `factor_report.py` | 单因子 HTML 报告 | `--factor momentum.ret_21d --pool 200` |
| `factor_scorecard.py` | **全量评级 + 评分卡入库** | `--source price --pool 200 --render` |
| `calibrate_directions.py` | 用实测 IC 校准方向 | `--min-abs-ic 0.02 --apply` |
| `research_reversal_density.py` | 反转信号密度阈值研究 | `--codes 500` |
| `research_reversal_regimes.py` | 分市场状态的最优阈值 | `--codes 500` |
| `research_composite_demo.py` | 复合因子 + 中性化演示 | — |
| `research_limitup_check.py` | 涨停可成交性检验 | `--pool 500` |

### 1.4 数据流

```
backfill_*.py  →  acquisition/data_interface  →  QuestDB
factor_*.py    →  factors/research/factor_engine  →  output/factor_reports + factor_scorecard
research_*.py  →  只读 QuestDB  →  output/research/*.csv
```

## 2. 核心用法与注意事项

### 2.1 常用命令

```bash
# 1) 财务因子物化（STORED，必须先跑才能给财务因子评级）
python scripts/backfill_factors.py --codes 120 --start 2024-01-01 --chunk 4

# 2) 全量评级（价量 85 个）
python scripts/factor_scorecard.py --source price --pool 200 --start 2019-01-01 --render

# 3) 全量评级（财务 61 个，读物化表）
python scripts/factor_scorecard.py --source financial --pool 200 --start 2024-01-01 --render

# 4) 方向校准（默认只报告，加 --apply 才写库）
python scripts/calibrate_directions.py --min-abs-ic 0.02

# 5) 单因子报告
python scripts/factor_report.py --factor momentum.ret_21d --pool 200
```

### 2.2 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **`--chunk` 控制内存** | `backfill_factors` 单批 4 只是内存安全线；调到 8+ 在百万行时会顶满 16G |
| **不要并发跑重任务** | 回填 + 评分卡同时跑会 OOM（曾经发生过）；串行执行 |
| **`--apply` 会改库** | `calibrate_directions --apply` 直接改 `factor_registry.direction`，先不带 `--apply` 看清单 |
| **输出位置固定** | 报告只写 `output/factor_reports/`，日志写 `output/research/`；不要往 `output/` 根目录扔文件 |
| **`backfill_daily_pre2022` 很慢** | 按日期全市场回填，约 5 小时；放夜间跑 |
| **脚本不自动注册因子** | 用到 `F.ts()` 的脚本要 `import factors`（导入即注册） |
| **重复执行** | 回填类脚本幂等（按缺失判断）；`init_database` 用 `CREATE TABLE IF NOT EXISTS` 也幂等 |
| **失败要看清退出码** | PowerShell 下 `python -u` 的 stderr 警告会显示 exit 1，但脚本可能已成功；看日志行数更可靠 |
