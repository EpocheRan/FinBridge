"""内置评估项（G 的第一批）。

每项都是独立函数 + ``@item`` 注册；新增评估项不需要改执行器、CLI 或报告。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.eval.metrics import (
    _independent_t,
    _mask_membership,
    _quantile_sets,
    rank_ic,
)

from .base import (
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    EvalContext,
    ItemResult,
    ItemSpec,
    item,
)


def _masked(ctx: EvalContext) -> pd.DataFrame:
    return _mask_membership(ctx.signal, ctx.membership)


# ---------------------------------------------------------------- 覆盖率
@item(
    ItemSpec(
        id="coverage",
        title="覆盖率与样本充分性",
        outputs=("coverage", "n_days", "avg_names"),
        default_params={"min_coverage": 0.6},
    )
)
def coverage(ctx: EvalContext, params: dict) -> ItemResult:
    signal = _masked(ctx)
    dates = ctx.values.index.intersection(signal.index)
    valid = (signal.loc[dates].notna() & ctx.forward.reindex(dates).notna())
    counts = valid.sum(axis=1)
    n_days = int((counts >= ctx.min_names).sum())
    total = max(1, len(ctx.panel_dates))
    ratio = n_days / total
    status = STATUS_OK if ratio >= float(params["min_coverage"]) else STATUS_DEGRADED
    note = "" if status == STATUS_OK else f"覆盖率 {ratio:.0%} < {params['min_coverage']:.0%}"
    return ItemResult(
        id="coverage",
        values={
            "coverage": ratio,
            "n_days": n_days,
            "avg_names": float(counts[counts > 0].mean()) if n_days else 0.0,
        },
        status=status,
        note=note,
    )


# ---------------------------------------------------------------- IC
@item(
    ItemSpec(
        id="ic",
        title="截面 IC / IR / 独立 t",
        requires=("coverage",),
        outputs=("ic", "ic_ir", "t_indep", "n_indep"),
        artifacts=("ic_series",),
        default_params={"min_ic": 0.02, "min_t": 2.0},
    )
)
def ic(ctx: EvalContext, params: dict) -> ItemResult:
    series = rank_ic(_masked(ctx), ctx.forward, ctx.min_names).dropna()
    if series.empty:
        return ItemResult.unavailable("ic", "无有效 IC 样本")
    mean = float(series.mean())
    std = float(series.std())
    t_indep, n_indep = _independent_t(series, ctx.horizon)
    status, note = STATUS_OK, ""
    if abs(mean) < float(params["min_ic"]):
        status, note = STATUS_DEGRADED, f"|IC| {abs(mean):.3f} < {params['min_ic']}"
    elif not np.isfinite(t_indep) or abs(t_indep) < float(params["min_t"]):
        status, note = STATUS_DEGRADED, f"独立 t {t_indep:.2f} < {params['min_t']}"
    return ItemResult(
        id="ic",
        values={
            "ic": mean,
            "ic_ir": mean / std if std else float("nan"),
            "t_indep": t_indep,
            "n_indep": n_indep,
        },
        artifacts={"ic_series": series},
        status=status,
        note=note,
    )


# ---------------------------------------------------------------- 多空价差
@item(
    ItemSpec(
        id="spread",
        title="多空分位价差（毛/净）与多头夏普",
        requires=("ic",),
        outputs=("spread_gross", "spread_net", "long_sharpe", "n_periods"),
    )
)
def spread(ctx: EvalContext, params: dict) -> ItemResult:
    signal = _masked(ctx)
    dates = list(ctx.panel_dates.intersection(signal.index)[:: max(1, int(ctx.horizon))])
    gross: list[float] = []
    longs: list[float] = []
    for dt in dates:
        if dt not in ctx.forward.index:
            continue
        top, bottom = _quantile_sets(signal.loc[dt], ctx.quantile, ctx.min_names)
        if not top or not bottom:
            continue
        ret = ctx.forward.loc[dt]
        top_ret = float(ret.reindex(top).mean())
        bottom_ret = float(ret.reindex(bottom).mean())
        if np.isfinite(top_ret) and np.isfinite(bottom_ret):
            gross.append(top_ret - bottom_ret)
            longs.append(top_ret)
    if not gross:
        return ItemResult.unavailable("spread", "没有可计算的调仓期")
    mean_gross = float(np.mean(gross))
    cost = 2.0 * float(ctx.cost_bps) / 1e4
    long = pd.Series(longs, dtype="float64")
    per_year = 252.0 / max(1, int(ctx.horizon))
    sharpe = (
        float(long.mean() / long.std() * np.sqrt(per_year))
        if len(long) > 3 and float(long.std()) > 0
        else float("nan")
    )
    net = mean_gross - cost
    return ItemResult(
        id="spread",
        values={
            "spread_gross": mean_gross,
            "spread_net": net,
            "long_sharpe": sharpe,
            "n_periods": len(gross),
        },
        status=STATUS_OK if net > 0 else STATUS_DEGRADED,
        note="" if net > 0 else f"净价差 {net:+.2%} ≤ 0（成本 {cost:.2%}）",
    )


# ---------------------------------------------------------------- 换手
@item(
    ItemSpec(id="turnover", title="组合换手率", requires=("spread",), outputs=("turnover",))
)
def turnover(ctx: EvalContext, params: dict) -> ItemResult:
    signal = _masked(ctx)
    dates = list(ctx.panel_dates.intersection(signal.index)[:: max(1, int(ctx.horizon))])
    prev: dict[str, float] = {}
    values: list[float] = []
    for dt in dates:
        top, _ = _quantile_sets(signal.loc[dt], ctx.quantile, ctx.min_names)
        if not top:
            continue
        weights = {symbol: 1.0 / len(top) for symbol in top}
        if prev:
            names = set(weights) | set(prev)
            values.append(0.5 * sum(abs(weights.get(s, 0) - prev.get(s, 0)) for s in names))
        prev = weights
    if not values:
        return ItemResult.unavailable("turnover", "没有相邻调仓期")
    return ItemResult(id="turnover", values={"turnover": float(np.mean(values))})


# ---------------------------------------------------------------- 参照相关性
@item(
    ItemSpec(
        id="reference_rho",
        title="与参照因子的截面相关性",
        requires=("ic",),
        outputs=("rho_reference",),
        default_params={"max_rho": 0.9, "step": 5},
    )
)
def reference_rho(ctx: EvalContext, params: dict) -> ItemResult:
    other = ctx.extras.get("reference_signal")
    if other is None:
        return ItemResult.unavailable("reference_rho", "本 profile 未指定参照因子")
    if ctx.factor_id == ctx.extras.get("reference_id"):
        return ItemResult.unavailable("reference_rho", "参照因子即自身")
    signal = _masked(ctx)
    dates = list(ctx.panel_dates.intersection(signal.index))[:: int(params["step"])]
    values: list[float] = []
    for dt in dates:
        if dt not in other.index:
            continue
        pair = pd.DataFrame({"a": signal.loc[dt], "b": other.loc[dt]}).dropna()
        if len(pair) >= ctx.min_names:
            values.append(pair["a"].rank().corr(pair["b"].rank()))
    if not values:
        return ItemResult.unavailable("reference_rho", "没有共同样本")
    rho = float(np.nanmean(values))
    status = STATUS_OK if abs(rho) < float(params["max_rho"]) else STATUS_DEGRADED
    return ItemResult(
        id="reference_rho",
        values={"rho_reference": rho},
        status=status,
        note="" if status == STATUS_OK else f"与参照 ρ {rho:.2f} ≥ {params['max_rho']}",
    )


# ---------------------------------------------------------------- 分层单调性
@item(
    ItemSpec(
        id="mono",
        title="分层单调性（相邻组方向正确的比例）",
        requires=("ic",),
        outputs=("mono",),
        default_params={"groups": 5},
    )
)
def mono(ctx: EvalContext, params: dict) -> ItemResult:
    signal = _masked(ctx)
    groups = int(params["groups"])
    dates = list(ctx.panel_dates.intersection(signal.index)[:: max(1, int(ctx.horizon))])
    scores: list[float] = []
    for dt in dates:
        row = signal.loc[dt].dropna()
        ret = ctx.forward.loc[dt].reindex(row.index)
        pair = pd.DataFrame({"s": row, "r": ret}).dropna()
        if len(pair) < max(10, ctx.min_names // 2):
            continue
        ranked = pair["s"].rank(method="first")
        buckets = pd.qcut(ranked, groups, labels=False, duplicates="drop")
        means = pair.groupby(buckets)["r"].mean().sort_index()
        if len(means) < 2:
            continue
        diffs = np.diff(means.to_numpy())
        scores.append(float((diffs > 0).mean()))
    if not scores:
        return ItemResult.unavailable("mono", "没有足够样本做分组")
    value = float(np.mean(scores))
    return ItemResult(
        id="mono",
        values={"mono": value},
        status=STATUS_OK if value >= 0.6 else STATUS_DEGRADED,
        note="" if value >= 0.6 else f"单调性 {value:.0%} < 60%",
    )


# ---------------------------------------------------------------- IC 衰减
@item(
    ItemSpec(
        id="decay",
        title="IC 衰减（1/5/10/20 日）",
        requires=("ic",),
        outputs=("ic_1d", "ic_5d", "ic_10d", "ic_20d"),
    )
)
def decay(ctx: EvalContext, params: dict) -> ItemResult:
    panels = ctx.extras.get("forward_by_horizon") or {}
    if not panels:
        return ItemResult.unavailable("decay", "本 profile 未加载多周期收益")
    out: dict[str, float] = {}
    for key, forward in panels.items():
        series = rank_ic(_masked(ctx), forward, ctx.min_names).dropna()
        out[f"ic_{key}"] = float(series.mean()) if len(series) else float("nan")
    return ItemResult(id="decay", values=out)


# ---------------------------------------------------------------- 断言
@item(
    ItemSpec(
        id="assertions",
        title="自检（方向先验 / 样本量 / 复现）",
        requires=("ic",),
        outputs=("checks",),
        default_params={"min_days": 60},
    )
)
def assertions(ctx: EvalContext, params: dict) -> ItemResult:
    ic_result = ctx.results.get("ic")
    coverage = ctx.results.get("coverage")
    problems: list[str] = []
    if ic_result is None or ic_result.status == STATUS_UNAVAILABLE:
        problems.append("IC 不可用")
    else:
        value = float(ic_result.values.get("ic", 0.0))
        if ctx.direction and value * ctx.direction < 0:
            problems.append("IC 符号与族先验方向相反")
    if coverage is not None and coverage.values.get("n_days", 0) < int(params["min_days"]):
        problems.append(f"有效天数 {coverage.values.get('n_days')} < {params['min_days']}")
    return ItemResult(
        id="assertions",
        values={"checks": "warn: " + "; ".join(problems) if problems else "pass"},
        status=STATUS_OK if not problems else STATUS_DEGRADED,
        note="; ".join(problems),
    )


# ---------------------------------------------------------------- 评级
@item(
    ItemSpec(
        id="rating",
        title="评级（A/B/C/D，口径对齐 report/rating.py）",
        requires=("ic", "spread", "mono", "coverage"),
        outputs=("grade", "score", "time_score", "space_score", "quadrant"),
    )
)
def rating(ctx: EvalContext, params: dict) -> ItemResult:
    """评级不在这里重算——直接采用 ``research/report/rating.py`` 的结果。

    旧评级依赖完整报告上下文（分年、regime、滚动符号率、增量、否决项），
    在 item 里重写必然产生第二套口径。所以：
    - 由 ``factor_report.py`` 生成报告后把 rating 传进来 → 完全一致；
    - 批量模式没有报告上下文 → 明确 unavailable，不编一个假分数。
    """
    payload = ctx.extras.get("rating_from_report")
    if not payload:
        return ItemResult.unavailable(
            "rating", "批量模式无完整报告；评级以 factor_report.py 输出为准"
        )
    values = {key: payload.get(key) for key in ("grade", "score", "time_score", "space_score", "quadrant")}
    return ItemResult(
        id="rating",
        values=values,
        status=STATUS_OK,
        note=f"来源 report/rating.py（{payload.get('scorecard_version', 'v?')}）",
    )
