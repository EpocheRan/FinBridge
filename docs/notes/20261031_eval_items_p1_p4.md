# 评估项化改造 P1–P4（2026-10-31）

> 目标：把 `factor_report.py` 的“一个大函数”拆成**评估项目录 + profile + 执行器**。
> 加一项评估 = 加一个文件；profiles 存库可复用；报告只增不减。

## 交付

| 期 | 内容 | 状态 |
|---|---|---|
| P1 | `research/eval_items/`（目录/注册/执行器）+ 3 项（coverage/ic/spread）+ `eval_profile` 表 + `scripts/profile.py` | ✅ |
| P2 | 补齐 turnover / reference_rho / mono / decay / assertions / rating | ✅ |
| P3 | `factor_report.py --profile` + 报告注入「profile + 数据完整性申明 + 未启用项」+ `verify_equivalence.py` | ✅ |
| P4 | 新增 `stability_by_year`（`eval_items/stability.py`），验证“只加一个文件” | ✅ |

## 核心结构

```
research/eval_items/base.py     # ItemSpec / ItemResult / EvalContext / 注册表 / execute / schema_for
research/eval_items/items.py    # 内置 9 项
research/eval_items/stability.py# P4 示例项（新增评估只加这种文件）
research/profile.py             # F：profile 存取（QuestDB eval_profile，版本化）
research/pipeline.py            # 主链路：profile → panel → 因子值 → items → 表 + meta
scripts/profile.py              # --seed / --list / --show / --catalog / --save
scripts/factor_eval.py          # 批量入口（--profile）
scripts/factor_report.py        # 单因子深评入口（--profile，默认 default）
scripts/verify_equivalence.py   # 1e-9 等价性回放
```

## 评估项状态（报告只增不减）

`ok` / `degraded`（照常出数 + 黄色标注 + 原因）/ `unavailable`（出占位块，写明缺哪张表、哪段日期）。
未勾选项在报告末尾单列「本 profile 未启用」，不占用正文。

## 验收：等价性 32/32 通过

```
python scripts/verify_equivalence.py
→ 比对 32 项，失败 0 项，容差 1e-09（最大相对误差 1.5e-16）
```

覆盖 8 个因子的 `ic / spread_gross / spread_net / turnover`，与改造前口径逐位一致。

## 已知差异（必须知道）

1. `rating` 是**新口径 v1**，与旧 `report/rating.py` 的 A/B/C/D 尚未逐位对齐（旧口径依赖
   time/space 双维打分与否决项，P5 再对齐）；报告正文仍用旧评级，注入区块里的 item 状态不含旧评级。
2. `decay` 只在 profile 勾选时加载多周期收益；`quick` 不勾，避免多余成本。
3. profile 不含池子/区间/成本（按约定），同一 profile 可跑不同池子做对比。

## 测试

`tests/test_eval_items.py`（6 例）：目录注册、依赖排序、schema 去重、执行与失败隔离、
无参照时 `reference_rho` 降级、未知项报错。全库 **480 passed / 12 skipped**，
仍剩 2 个与本次无关的既有失败（moneyflow / screener）。
