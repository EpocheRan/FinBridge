# 因子落库/消费方式标记（materialization）设计

## 目标

回测时同一策略可以混用两类因子：

1. **读已预计算落库的因子**（`factor_value_daily`）。
2. **对没落库的因子逐 bar 现算**。

`acquisition.run --datasets factors` 只预计算**指定集合**，指定之外不落库。每个因子带 `materialization` 标记，引用方式不匹配时**启动期报错**，而不是静默 NaN。

## 核心：能力 vs 覆盖

- **能力（capability）**：这个因子有没有 compute 实现、能否被落库 —— 静态，写在 `FactorSpec.materialization`。
- **覆盖（availability）**：当前 codes × 日期段里是否**实际有**落库数据 —— 动态，回测时查 `load_factors` 结果判断。

## 标记

```python
class Materialization(str, Enum):
    STORED = "stored"   # 必须预计算；回测只读，缺数据 = 报错
    INLINE = "inline"   # 不落库；逐 bar 现算，必须有 compute
    AUTO   = "auto"     # stored 优先，缺失且有 compute 则回退 inline
```

- `FactorSpec.materialization`（引擎侧目录，持久化到 `factor_registry.materialization`）
- `FactorDef.materialization`（回测侧声明，`F.factor(id, materialization=...)`）

内置因子标记：

- `momentum.ret` / `reversal.short` / `technical.atr` / `risk.vol` → `AUTO`
- `liquidity.turnover` → `STORED`（依赖 `turnover` 列，回测 frame 无此列，只能读落库）
- 23 个 legacy（`technical.*`）→ `STORED`（现算依赖宽表引擎 + 基准 + 参考数据，不在回测 frame 内）

## 运行时解析（决策矩阵）

有效模式 = `max(因子级 materialization, 全局 factor_source)`：

| 因子标记 | 落库数据有？ | 行为 |
|---|---|---|
| `STORED` | ✅ | 读 stored |
| `STORED` | ❌ | **启动期报错**，提示先跑 `acquisition ... --factor <id>` |
| `INLINE` | — | 逐 bar 现算（`INLINE_COMPUTE_REGISTRY`） |
| `INLINE` 且无 compute | — | 报错 |
| `AUTO` | ✅ | 读 stored |
| `AUTO` | ❌ 且有 compute | 现算 + `warning` |
| `AUTO` | ❌ 且无 compute | 报错 |

全局 `factor_source`：

- `auto`：按因子标记逐因决定（默认）
- `stored`：全部强制 `STORED`；op 类现算因子（`F.mom` 等）直接报错
- `compute`：全部强制 `INLINE`；无 compute 的因子报错

## 预计算白名单（acquisition）

- `Scope.factor_ids`（`acquisition run/update --factor a,b,c`）控制本次只预计算哪些因子。
- `FactorEngineService.default_precompute_ids()` 返回 `STORED + AUTO` 集合。
- `syncers.factors`：`scope.factor_ids` 为空时用默认集合。

```bash
# 只预计算这三个因子（其余不落库）
python -m acquisition update --code 600519 \
  --start 2026-10-01 --end 2026-10-27 \
  --datasets factors --factor momentum.ret,technical.atr,technical.pct_chg
```

## 回测侧实现位置

- `backtest/application/runner.py`：
  - `_effective_mode` / `_canonical_id` / `_build_stored_frame` / `_compute_inline`
  - `_build_stored_frame` 一次规划：stored 读库、auto 缺则回退、inline 现算，最终合并成 long frame 交给 `build_panel`。
- `factor_engine/definitions/__init__.py`：
  - `INLINE_COMPUTE_REGISTRY`：可逐 bar 现算的 `factor_id -> BuiltinFactor`
  - `register_all()`：内置 + legacy 幂等注册到全局 registry（回测侧依赖它解析 alias/canonical）。

## 错误信息示例

```text
ConfigError: 因子 technical.pct_chg 标记 STORED，但 2026-10-01..2026-10-27 缺落库数据
（codes=['600519']）。请先运行：
python -m acquisition update --code 600519 --start 2026-10-01 --end 2026-10-27 --datasets factors --factor technical.pct_chg
```

## 兼容

- 老策略 `F.factor(id)` 默认 `AUTO`，行为不变。
- `F.mom/ma/atr/vol` 仍是 op 类现算。
- `F.stored(column)` legacy 保持，映射 `factor_daily_wide`。
