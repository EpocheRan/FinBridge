# 评估体系 v3：全市场、评估项化、报告层零 SQL（2026-10-31）

> 本文是本次改造的收口记录。取代 `20261031_unified_eval.md`（已删除）。
> 关键结果：**报告从 600 s 降到 170 s，口径统一成全市场，评级不变（B / 72.2）。**

## 1. 最终架构（A–G 落位）

| 模块 | 职责 | 代码 |
|---|---|---|
| A 配方 | 因子定义 / 算子 / 依赖 | `factors/`、`factor_engine/definitions/` |
| B 数据 | **唯一取数入口**，按需装配 6 大数据域 | `research/eval/data.py::load_panel` |
| C 计算 | factor_id → 因子面板 | `research/eval/compute.py::factor_panel` |
| D 评估 | 按目录执行评估项 | `research/eval_items/base.py` + `items.py` |
| E 报告 | 渲染 + 注入 profile/完整性申明 | `research/report/*` + `panel_adapter.py` |
| F 评估内容 | profile 存取（存库、版本化） | `research/profile.py`（表 `eval_profile`） |
| G 评估项 | 每项一个文件 | `research/eval_items/items.py`、`stability.py` |

主链路：`research/pipeline.py::run()`，两个 CLI（`factor_eval.py` / `factor_report.py`）共用。

## 2. 数据与默认口径

- **默认全市场**：`EvalSpec.universe = "all_a"`（区间内全部 `type='stock'`，约 5,863 只）；
  另有 `index:000905` / `pool:日期:N` / `explicit:` 三种写法；
- 报告层**不再取数**：`report/panel.py` 的 244 行 SQL 全删，`load_panel` 只做 3 行委托；
- 取数**一次**：主面板（全市场）与 3 个稳健性池共用一份超集，稳健性只在内存切片；
- 成分口径：`index:` 走 `index_weight` PIT 快照；`all_a` 以数据可得性为约束；`pool:` 为基准日成交额前 N。

## 3. 评估项（G）与 profile（F）

10 个评估项：coverage / ic / spread / turnover / mono / decay / reference_rho /
assertions / rating / stability_by_year。

- 加一项 = 加一个文件 + 在 `eval_items/__init__.py` import 一次；
- profile 只存「评估什么」（items / params / thresholds / reference），**不含池子、区间、成本**；
- `default` = 深评 10 项，`quick` = 5 项快筛；用户可 `scripts/profile.py --save` 自定义并存库；
- 新增评估项只需勾选，D 自动执行、E 自动渲染、CSV 自动多列。

报告四态：`ok` / `degraded`（出数 + 标注原因）/ `unavailable`（占位 + 缺哪张表哪段日期）/
`not_selected`（末尾单列）。**缺数据只申明，不删内容。**

## 4. 性能（全市场，2020-01 起）

| 轮次 | 改动 | 耗时 |
|---|---|---|
| 起点 | 5 次取数 + `pivot_table` + 逐日 Series | ~600 s |
| P1 | 超集一次取数 + 稳健性内存切片 | 423 s |
| P2 | `pivot_table` → `pivot` | — |
| P3 | `_quantile_nav` 内层 numpy 化（17 s → 1.3 s/次） | **170 s** |

## 5. 验收

| 项 | 结果 |
|---|---|
| 评级（改造前后） | B / 72.2（79 × 90）完全一致 |
| 统一评估等价性 | `verify_equivalence.py` 32/32，最大相对误差 1.5e-16（容差 1e-9） |
| 测试 | 475 passed / 12 skipped（2 个既有无关失败） |
| 报告层 SQL | 0 |
| 报告头部 | 数据 / 副标题 / banner / 摘要 / 漏斗 全部 `all_a` |

## 6. 遗留

1. `--pool` 已不影响主报告，只作用于稳健性内部池，建议改名或删除；
2. `verify_equivalence.py` 仍以旧 eval CSV 为基线，可加「报告 vs eval」一项；
3. STORED 因子（财务/估值）在批量模式下 `rating` 为 `unavailable`（无报告上下文），
   评级只以 `factor_report.py` 输出为准；
4. `report/panel.py` 仍保留 `Panel` 数据类与 `peer_panels`，取数已迁出。

## 7. 站点页（docs/index.html）同步

三处与代码对齐，均为字节级替换（`git diff --numstat` 13/9）：

1. **系统架构图** research 节点：标题「因子研究 → 因子评估」、副标题改为
   `eval_items · profile · render`、标签改为「评估项 + profile」、底部改为「全市场 · 10 评估项」；
2. **模块卡片** research：`cn` 改为「因子评估」，`role` 改为「统一评估 + 报告层不取数」，
   `funcs` 换成 `pipeline.run / eval_items.execute / profile.load / generate / verify_equivalence`，
   `deps` 补 `eval/data.py`，`tables` 补 `eval_profile`；
3. **数据模型**：新增 `eval_profile`（domain=factor，writer=research），字段为
   `updated_at / name / version / items / item_params / thresholds / reference / profile_hash`。

备注：模块与数据模型两个视图由 JS 数据数组渲染，改的是数据而非逻辑；
若视图白屏，先查 `fields:[...]` 的引号与逗号。
