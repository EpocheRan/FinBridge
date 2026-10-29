# 03 · factors（因子定义与计算）

## 1. 架构与依赖

### 1.1 职责边界

`factors` 是**纯计算层**：给定一张日线 DataFrame，算出因子序列；或给定多标的宽表，
构建 `date × symbol` 的因子面板。它**不碰数据库、不读配置、不发网络请求**（唯一例外是
`research` 里读库的编排，不在本模块）。

设计目标：同一份因子定义，既能被 `build_panel` 现算（回测/研究），也能被
`factor_engine` 物化落库（选股/横截面），**口径完全一致**。

### 1.2 上下游

| 方向 | 模块 | 关系 |
|---|---|---|
| 上游 | 无 | ★ 不 import 任何业务模块 |
| 下游 | `factor_engine` | 读取定义、调 `build_panel` / `compute_series` |
| 下游 | `strategies`、`backtest`、`screener`、`position` | 用 `F.*` 声明因子 |
| 数据 | `build_panel` 的入参 | 由调用方查库后传入 DataFrame |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `library.py` | `FactorDef`（因子定义）+ `F`（声明式构造器）+ `@factor` 注册装饰器 |
| `spec.py` | `FactorSpec`（元数据：分类/来源/窗口/单位/方向/物化方式） |
| `registry.py` | 全局 registry：注册、别名解析、拓扑排序、hash |
| `panel.py` | `build_panel`：多标的 → 面板；`FactorPanel` / `FactorPanelView` |
| `ops/__init__.py` | `compute_series`（算子分发）、`clean_series`、`CUSTOM_FACTORS` |
| `ops/ts.py` | 时序算子：`pct_change`/`rolling_mean`/`atr_pct`/`rolling_beta`/`volume_stats`… |
| `ops/price_factors.py` | 价量因子库 85 个算子（liquidity/momentum/reversal/risk/size） |
| `cross_section.py` | 横截面变换：`rank_pct`/`zscore`/`winsorize`/`composite`/`neutralize`/`nonlinear_size` |
| `naming.py` | 因子命名规范 `house_id(category, name)` |
| `taxonomy.py` | `Category` 枚举 + 子维度白名单 |
| `stored.py` | `StoredColumn`：从 `screener_daily` 直接取列的「存储因子」 |
| `technical/atr.py` | ATR 实现（Wilder） |
| `engine.py` | `IndicatorEngine`：PandasTA 风格的指标容器 |

### 1.4 数据流

```
F.mom(20) ─┐
F.rank("mom20") ─┤         ┌─────────────┐
F.composite([...]) ─┼──►  FactorDef(列表)  ──►  build_panel(frame, defs, start, end)
F.stored("pe_ttm") ─┘         └─────────────┘            │
                                                        ▼
                                              FactorPanel(date × symbol)
```

## 2. 核心类与方法

### 2.1 类/函数表

| 名称 | 签名 | 说明 |
|---|---|---|
| `FactorDef` | dataclass | 因子定义：`name/op/args/inputs/on/window/warmup/unit/source/price_basis` |
| `F.mom(n)` | → `FactorDef` | N 日动量 `close/close.shift(N)-1` |
| `F.ma(n)` | → `FactorDef` | N 日均线 |
| `F.atr(n)` / `F.vol(n)` | → `FactorDef` | ATR% / 已实现波动率 |
| `F.stored(col)` | → `FactorDef` | 直接取 `screener_daily` 的列 |
| `F.factor(id)` | → `FactorDef` | 按注册 ID 引用（含别名解析） |
| `F.rank(name)` / `F.zscore(name)` | → `FactorDef` | 派生：横截面排名 / 标准化 |
| `F.composite(parts, ...)` | → `FactorDef` | 多因子合成（带权重/方向/去极值/中性化） |
| `F.neutralize(name)` | → `FactorDef` | 行业+市值中性化 |
| `F.ts(factor_id)` | → `FactorDef` | 引用价量因子库（如 `momentum.ret_21d`） |
| `F.custom(op)` | → `FactorDef` | 引用 `CUSTOM_FACTORS` 里的自定义算子 |
| `build_panel` | `(frame, defs, start, end, *, stored=None, reference=None)` | 构建面板 |
| `FactorPanel` | `.values`（dict[name→DataFrame]）、`.at(dt, name)`、`.get(symbol, name, dt)`、`.view(dt)` | 面板容器 |
| `compute_series` | `(defn, data)` | 单标的算子分发 |
| `register_factor` / `resolve_id` / `list_factors` / `topological_order` / `default_precompute_ids` | — | registry 操作 |

### 2.2 用法

```python
from factors import F, build_panel, register_factor

defs = [
    F.mom(20),
    F.rank("mom20"),
    F.composite([("mom20", -1, 0.5), ("vol20", 1, 0.3), ("atr14", -1, 0.2)],
                name="mom_rev_combo", winsorize=0.02, neutralize=("industry", "size")),
    F.ts("risk.beta_60d_csi300"),
]
panel = build_panel(frame, defs, "2024-01-01", "2026-10-28",
                    reference={"industry": ind_df, "size": size_df})
combo = panel.values["mom_rev_combo"]        # DataFrame(date × symbol)
```

```python
# 横截面变换
from factors.cross_section import rank_pct, composite, neutralize, nonlinear_size

score = composite({"mom": mom, "vol": vol}, weights={"mom": 0.6, "vol": 0.4})
score = neutralize(score, industry=ind, size=size)
```

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **`factors` 不能引入 DB** | 一旦 import `data_interface`，单测就需要活库，且破坏分层 |
| **warmup 必须够** | `F.mom(250)` 前 250 行必然 NaN；`required_warmup(defs)` 会自动汇总，但调用方要给足数据 |
| **`composite` 的缺失处理** | 默认 `min_parts` 控制"至少几个子因子有值才计算"；权重会按可用部分**重新归一**，不会零填充 |
| **`neutralize` 需要 `reference`** | `build_panel(..., reference={...})` 未传时，带中性化的 composite 会抛 `FactorError`（不静默） |
| **`rank` 是 pct rank** | 返回 `(0,1]`，最小值是 `1/n` 而不是 0；比较阈值时注意 |
| **方向要自己统一** | `F.composite` 里用 `direction` 取符号；单独的 `F.rank` 不会替你翻方向 |
| **`F.ts` 的 ID 是自研命名** | 不是 `return_21d`，而是 `momentum.ret_21d`；见 `factors/naming.py` |
| **`F.stored` 依赖 screener 列** | 列不存在时返回全 NaN，不会报错——检查 `SCREENER_DAILY_COLUMNS` |
| **registry 有别名** | `resolve_id("mom20")` → `momentum.ret@20`；直接比较字符串会踩空 |
| **`price_basis`** | 声明 `hfq` 的因子必须传后复权价，混用原始价会得到错误的分红/拆股跳变 |
