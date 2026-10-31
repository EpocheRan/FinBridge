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
| `backfill_history.py` | **历史数据补齐（分阶段）** | 见下表 |
| `factor_report.py` | 单因子 HTML 报告 | `--factor momentum.return_21d --pool 200` |
| `factor_scorecard.py` | **全量评级 + 评分卡入库** | `--source price --pool 200 --render` |
| `calibrate_directions.py` | 用实测 IC 校准方向 | `--min-abs-ic 0.02 --apply` |
| `research_reversal_density.py` | 反转信号密度阈值研究 | `--codes 500` |
| `research_reversal_regimes.py` | 分市场状态的最优阈值 | `--codes 500` |
| `research_composite_demo.py` | 复合因子 + 中性化演示 | — |
| `research_limitup_check.py` | 涨停可成交性检验 | `--pool 500` |
| `compact-docker-vhdx.ps1` | **重建 Docker 数据盘**（回收 vhdx 空转空间） | 管理员 PowerShell，见 §1.3.1 |

### 1.3.1 Docker 数据盘回收（PowerShell）

QuestDB 跑在 Docker 里，数据存在 WSL2 的数据盘 `docker_data.vhdx`（默认 `D:\DockerData\`）。该文件
**只增不减**：删除数据后空间不会自动归还，文件体积会远大于实际占用（实测：文件 400 GB，实际只用 13 GB）。

**注意：`diskpart compact` + `wsl --manage --set-sparse` 这套是无效的**：

1. `wsl --manage docker-desktop --set-sparse` 只作用于**发行版根盘**
   （`C:\Users\<user>\AppData\Local\Docker\wsl\main\ext4.vhdx`，通常 <1 GB），不是那个几百 GB 的数据盘；
2. 数据盘不是稀疏文件，guest 里的 TRIM/fstrim 会被直接丢弃；
3. `compact vdisk` 只能回收「内容为零」的块，而 ext4 释放的块里仍是旧数据，对 compact 来说依然算已分配。

唯一可靠的办法是**重建数据盘**（脚本已按此实现）：

```powershell
# 1) 先干跑：只做检查、打印计划与预计回收空间，不停止任何东西
powershell -ExecutionPolicy Bypass -File D:\FinBridge\scripts\compact-docker-vhdx.ps1 -DryRun

# 2) 管理员 PowerShell 正式执行（需要输入 YES 确认）
powershell -ExecutionPolicy Bypass -File D:\FinBridge\scripts\compact-docker-vhdx.ps1
```

脚本流程：停容器 → 备份命名卷到 `D:\docker-backup`（带 tar 条目数校验）→ `compose down` /
退出 Docker Desktop / `wsl --shutdown` → 删除 vhdx → `wsl --unregister docker-desktop` →
启动 Docker（自动重建约 1 GB 数据盘）→ 恢复卷 → `compose up -d` → 校验 `ohlcv_1d` 行数与压缩前一致。

- 会中断 Docker / QuestDB 约 10~20 分钟，**数据不会丢**（先备份再删，恢复后校验行数）；
- 备份需要约「卷大小」的空闲空间（当前 QuestDB 卷约 13 GB）；
- 备份目录**不会自动删除**，确认服务正常后手动清理；
- 脚本必须存为 **UTF-8 with BOM**，否则 PowerShell 5.1 会把中文读成乱码并报语法错。

### 1.4 数据流

```
backfill_*.py  →  acquisition/data_interface  →  QuestDB
factor_*.py    →  factors/research/factor_engine  →  output/factor_reports + factor_scorecard
research_*.py  →  只读 QuestDB  →  output/research/*.csv
```


### 1.3.2 历史数据补齐（`backfill_history.py`）

按阶段补齐历史数据，每阶段独立、可中断、幂等重跑。

| 阶段 | 内容 | 数据量 | 耗时 |
|---|---|---|---|
| `p0-codefix` | 修 5 只与指数同码的个股（000001/000016/000688/000852/000905） | 3 万行 | 1 分钟 |
| `p0-daily` | 按交易日补全市场日线（`daily(trade_date=)` 一次拿全市场） | 379 万行 | ~45 分钟 |
| `p1-delisted` | 退市股历史行情 | 20 万行 | 15 分钟 |
| `p1-index` | 历史指数成分（`index_weight` 按月切块） | 25.6 万行 | 10 分钟 |
| `p2-structure` | 龙虎榜/两融/大宗（按日） | 600 万行 | ~60 分钟 |
| `p2-periodic` | 解禁/回购/股东户数（按月分页） | 86 万行 | 5 分钟 |
| `p3-financial` | 财务三表 + 指标（按代码，2010 起） | 待测 | ~6 小时 |

```bash
python scripts/backfill_history.py --phase p0-codefix
python scripts/backfill_history.py --phase p0-daily --start 2019-01-01 --end 2022-10-09
python scripts/backfill_history.py --phase p1-delisted
python scripts/backfill_history.py --phase p1-index
python scripts/backfill_history.py --phase p2-structure --start 2019-01-01 --end 2026-10-29
python scripts/backfill_history.py --phase p2-periodic --start 2019-01-01 --end 2026-10-29
python scripts/backfill_history.py --phase p3-financial --start-year 2010
```

**实现要点与坑**：

1. **按日拉取**比按代码快 5 倍：`daily(trade_date=...)` 一次返回全市场 4,700 行；
2. **单位换算**：Tushare `vol`（手）×100 → 股，`amount`（千元）×1000 → 元；
3. **DDL 响应无 `dataset` 字段**，REST 解析要 `.get("dataset") or []`；
4. **`_native` 不能把普通字符串当日期解析**：`'000001'` 会被 pandas 解析成 2000-01-01，
   导致 `TIMESTAMP → SYMBOL` 类型错误；
5. **退市股 volume 常为 NaN**，QuestDB 的 LONG 列拒绝 NaN，写前要 `fillna(0).astype(int64)`；
6. **区间类接口单次上限 6,000 行**，解禁/回购/股东户数必须按月分页。

补齐后的数据覆盖见 [../data/coverage.md](../data/coverage.md)。

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
python scripts/factor_report.py --factor liquidity.amount_ma_20d --pool 200 --start 2019-01-01
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
