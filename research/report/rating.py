"""因子评级：100 分制评分卡 + 一票否决 + 二维定位。

本版（2026-10 重写）修正四类结构性问题：

1. **方向**：不再按样本内 IC 符号翻转；``report.direction`` 是事先给定的先验，
   负 IC 记 0 分并触发「方向与先验相反」，而不是把因子翻成正 IC 再给高分。
2. **预测力**：改为连续加权（IC 幅度 60% + ICIR 40%），不再用 ``min()`` 封顶，
   避免 90% 的因子挤在 0–12 分。
3. **稳定性**：分年 CI 显著率 40% + 分状态 CI 显著率 30% + 滚动 12 个月 IC 同号率 30%，
   不再只靠 3 个 regime 拿不满 12.5 分。
4. **可交易性**：用毛价差的 95% 置信区间与单期成本比较（上界<成本→触发；
   下界>成本→通过；否则边界），不再用「点估计与成本差 1.96SE」的死区。
"""

from __future__ import annotations

import numpy as np

from . import diagnostics as diag

GRADE_LINES = (("A", 85.0), ("B", 70.0), ("C", 55.0))
SCORECARD_VERSION = "v2.1-2026-10"


def _band(value: float, bands: list[tuple[float, float]], default: float) -> float:
    if value != value:
        return default
    for threshold, score in bands:
        if value >= threshold:
            return score
    return default


def _grade(total: float) -> str:
    for label, line in GRADE_LINES:
        if total >= line:
            return label
    return "D"


def _clip01(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(max(0.0, min(1.0, value)))


def _rolling_sign_ratio(report, ic_mean: float) -> float:
    """滚动 12 个月 IC 均值与全样本同号的比例（#19）。"""
    series = report.ic.get(5)
    if series is None or len(series) < 260 or not np.isfinite(ic_mean):
        return float("nan")
    rolled = series.rolling(252, min_periods=126).mean().dropna()
    if len(rolled) < 12:
        return float("nan")
    if ic_mean >= 0:
        return float((rolled > 0).mean())
    return float((rolled < 0).mean())


def rate(report) -> dict:
    stats = report.ic_stats
    primary_h = 5 if 5 in stats.index else (stats.index[0] if len(stats) else None)
    ic_mean = float(stats.loc[primary_h, "mean"]) if primary_h is not None else float("nan")
    icir = float(stats.loc[primary_h, "ir"]) if primary_h is not None else float("nan")
    nw_t = float(stats.loc[primary_h, "t"]) if primary_h is not None else float("nan")
    t_indep = float(stats.loc[primary_h, "t_indep"]) if primary_h is not None else float("nan")
    ic_win = float(stats.loc[primary_h, "win"]) if primary_h is not None else float("nan")
    n_years = int(len(report.ic_by_year.dropna())) if report.ic_by_year is not None else 0

    # ---- 预测力 30：连续加权，不再 min() 封顶
    ic_pos = max(0.0, ic_mean) if np.isfinite(ic_mean) else 0.0
    icir_pos = max(0.0, icir) if np.isfinite(icir) else 0.0
    power = 30.0 * (0.6 * _clip01(ic_pos / 0.05) + 0.4 * _clip01(icir_pos / 0.50))

    # ---- 时间稳定性 25：分年 40 + 分状态 30 + 滚动 12m 同号 30
    year_ratio = float("nan")
    if report.ic_by_year is not None and len(report.ic_by_year) >= 2:
        signs = np.sign(report.ic_by_year.dropna().to_numpy())
        year_ratio = float(np.mean(signs > 0)) if ic_mean >= 0 else float(np.mean(signs < 0))
    year_ci_ratio = float("nan")
    sig_years: list[tuple[int, float]] = []
    if report.ic_by_year_ci is not None and len(report.ic_by_year_ci) >= 2:
        frame = report.ic_by_year_ci.dropna(subset=["ci_lo", "ci_hi"])
        if len(frame):
            mask = (frame["ci_lo"] > 0) | (frame["ci_hi"] < 0)
            year_ci_ratio = float(mask.mean())
            sig_years = [(int(y), float(rec["mean"])) for y, rec in frame[mask].iterrows()]
    regime_ratio = float("nan")
    if report.ic_by_regime is not None and len(report.ic_by_regime.dropna()) >= 2:
        vals = report.ic_by_regime.dropna().to_numpy()
        regime_ratio = float(np.mean(vals > 0)) if ic_mean >= 0 else float(np.mean(vals < 0))
    regime_ci_ratio = float("nan")
    if report.regime_stats is not None and len(report.regime_stats.dropna(subset=["ci_lo", "ci_hi"])) >= 2:
        frame = report.regime_stats.drop(index="牛市−熊市", errors="ignore").dropna(subset=["ci_lo", "ci_hi"])
        if len(frame):
            regime_ci_ratio = float(((frame["ci_lo"] > 0) | (frame["ci_hi"] < 0)).mean())
    rolling_ratio = _rolling_sign_ratio(report, ic_mean)

    year_base = year_ci_ratio if np.isfinite(year_ci_ratio) else (year_ratio if np.isfinite(year_ratio) else 0.5)
    regime_base = regime_ci_ratio if np.isfinite(regime_ci_ratio) else (regime_ratio if np.isfinite(regime_ratio) else 0.5)
    rolling_base = rolling_ratio if np.isfinite(rolling_ratio) else year_base
    stability = 25.0 * (0.4 * year_base + 0.3 * regime_base + 0.3 * rolling_base)

    # ---- 空间单调性 15（用毛收益的 ρ；负方向/不单调不给分）
    mono = float(report.monotonicity) if np.isfinite(report.monotonicity) else 0.0
    monotonicity = _band(
        mono,
        [(0.80, 15.0), (0.60, 10.0), (0.40, 6.0), (0.00, 3.0)],
        0.0,
    )
    if ic_mean < 0:
        monotonicity = 0.0

    # ---- 可交易性 20：覆盖率 6 + 换手 6 + 净价差 t 8
    coverage_points = _band(report.coverage, [(0.85, 6.0), (0.70, 3.5)], 1.0)
    turnover_points = _band(
        -(report.turnover if np.isfinite(report.turnover) else 1.0),
        [(-0.30, 6.0), (-0.50, 3.5)],
        1.0,
    )
    net_t = float((report.spread_net or {}).get("t", np.nan))
    spread_points = _band(net_t if np.isfinite(net_t) else -1.0, [(2.0, 8.0), (1.0, 5.0), (0.0, 3.0)], 0.0)
    trade_score = coverage_points + turnover_points + spread_points

    # ---- 增量性 20：残差保留率 14 + 分散度 6（原 15 分制 ×4/3）
    inc = report.incrementality or {}
    if inc.get("score") is not None:
        incremental = float(inc["score"]) * (20.0 / 15.0)
    else:
        incremental = 10.0
    ratio = float(inc.get("effective_ratio", inc.get("ratio", np.nan)))
    max_corr = float(inc.get("max_corr", np.nan))
    incremental_note = (
        diag.retention_note(ratio, max_corr, float(inc.get("ic_raw", np.nan)))
        if inc
        else "未接入同类因子相关性，暂给中性分 10/20（非实测）"
    )

    # ---- 合维：预测力 + 稳定性并为一维 45，避免同一弱点被扣两次（#20）
    combined = (power + stability) * (45.0 / 55.0)
    items = [
        ("预测力与稳定性", round(combined, 1), 45.0),
        ("空间单调性", round(monotonicity, 1), 15.0),
        ("可交易性", round(trade_score, 1), 20.0),
        ("增量性", round(incremental, 1), 20.0),
    ]
    total = sum(score for _, score, _ in items)
    raw_grade = _grade(total)

    # ---- 一票否决
    n_sig = len(sig_years)
    sig_desc = "、".join(f"{year}{'−' if mean < 0 else '+'}" for year, mean in sig_years) or "无"
    direction_mixed = len({1 if mean > 0 else -1 for _, mean in sig_years}) > 1
    financial = str(report.category) in ("quality", "value", "growth", "valuation")
    ic_threshold = 0.010 if financial else 0.015
    min_sig_years = 2 if (financial and n_years < 5) else 3
    insufficient = bool(
        (not np.isfinite(ic_mean)) or abs(ic_mean) < ic_threshold
        or n_sig < min_sig_years or direction_mixed
    )

    trade_check = diag.tradeability_interval(report.spread_gross or {}, report.cost_mean)
    if trade_check.status == "fail":
        trade_status = "触发"
    elif trade_check.status == "warn":
        trade_status = "边界（区间跨成本）"
    elif trade_check.status == "pass":
        trade_status = "通过"
    else:
        trade_status = "—"
    t_dir = t_indep if np.isfinite(t_indep) else nw_t
    direction_bad = bool(
        np.isfinite(ic_mean) and ic_mean < -0.010
        and ((not np.isfinite(t_dir)) or t_dir < -2)
    )
    veto_rules = [
        {
            "rule": "证据不足",
            "threshold": f"|IC|<{ic_threshold:.3f}，或显著年份<{min_sig_years}，或显著年份方向不一致",
            "actual": f"IC {ic_mean:+.3f}；显著年份 {n_sig} 个（{sig_desc}）；样本 {n_years} 年",
            "triggered": insufficient,
            "status": "触发" if insufficient else "通过",
        },
        {
            "rule": "方向与先验相反",
            "threshold": "IC<−0.010 且独立 t<−2（不自动翻转）",
            "actual": f"IC {ic_mean:+.3f}，独立 t {t_dir:+.1f}",
            "triggered": direction_bad,
            "status": "触发" if direction_bad else "通过",
        },
        {
            "rule": "覆盖率不足",
            "threshold": "< 70%",
            "actual": f"{report.coverage:.1%}",
            "triggered": bool(report.coverage < 0.70),
            "status": "触发" if report.coverage < 0.70 else "通过",
        },
        {
            "rule": "分层反向",
            "threshold": "单调性 ρ < 0",
            "actual": f"{mono:.2f}",
            "triggered": bool(mono < 0),
            "status": "触发" if mono < 0 else "通过",
        },
        {
            "rule": "不可交易",
            "threshold": "毛价差 95% 上界 < 单期成本 → 触发；下界 > 成本 → 通过；否则边界",
            "actual": trade_check.actual,
            "triggered": trade_check.status == "fail",
            "status": trade_status,
        },
        {
            "rule": "换手过高（每期）",
            "threshold": "> 80%",
            "actual": f"{report.turnover:.0%}" if np.isfinite(report.turnover) else "—",
            "triggered": bool(np.isfinite(report.turnover) and report.turnover > 0.80),
            "status": "触发" if np.isfinite(report.turnover) and report.turnover > 0.80 else "通过",
        },
    ]
    vetoes = [f"{v['rule']}：{v['actual']}（阈值 {v['threshold']}）" for v in veto_rules if v["triggered"]]

    grade = raw_grade
    capped = False
    if vetoes and grade in ("A", "B"):
        grade = "C"
        capped = True

    # ---- 二维定位
    time_score = 100.0 * (0.4 * year_base + 0.3 * regime_base + 0.3 * rolling_base)
    space_score = 100.0 * _clip01(0.7 * max(mono, 0.0) + 0.3 * min(1.0, ic_pos / 0.06))
    if time_score >= 60 and space_score >= 60:
        quadrant = ("核心因子", "可直接进多因子合成，权重 15%~25%")
    elif time_score < 60 and space_score >= 60:
        quadrant = ("择时因子", "空间区分度强但状态依赖，只在特定 regime 启用")
    elif time_score >= 60 and space_score < 60:
        quadrant = ("过滤因子", "适合做负面剔除/风控条件，不做打分")
    else:
        quadrant = ("淘汰", "时间与空间均不达标，归档并记录失效原因")

    filter_note = ""
    if report.filter_test and not report.filter_test.get("useful", False):
        filter_note = (
            "过滤价值未通过：Q1 绝对收益为正且与全组接近，剔除后夏普改善不足 0.05；"
            "不能把「过滤」作为使用理由。"
        )
    attribution = (
        report.attribution if grade == "D"
        else {"label": "—", "reason": "非 D 级，不适用失效归因"}
    )
    notes = [incremental_note]
    if filter_note:
        notes.append(filter_note)
    if grade == "D":
        notes.append(
            f"D 级失效归因：{attribution.get('label')}（{attribution.get('reason')}）；"
            "归档，不建议单独使用或做过滤。"
        )
    if direction_bad:
        notes.append(
            "方向与先验相反：本报告**没有**按样本内 IC 翻转。若要用反向信号，"
            "需在独立训练窗口重新校准并写入 registry，再以新方向重跑本报告。"
        )

    return {
        "score": round(total, 1),
        "grade": grade,
        "raw_grade": raw_grade,
        "capped": capped,
        "items": items,
        "vetoes": vetoes,
        "veto_table": veto_rules,
        "grade_lines": GRADE_LINES,
        "time_score": round(time_score, 1),
        "space_score": round(space_score, 1),
        "quadrant": quadrant[0],
        "usage": quadrant[1],
        "attribution": attribution,
        "scorecard_version": SCORECARD_VERSION,
        "detail": {
            "icir": icir,
            "nw_t": nw_t,
            "t_indep": t_indep,
            "ic_win": ic_win,
            "ic_mean": ic_mean,
            "n_years": n_years,
            "n_sig_years": n_sig,
            "power": round(power, 2),
            "stability": round(stability, 2),
            "monotonicity": mono,
            "spread_gross_mean": (report.spread_gross or {}).get("mean", np.nan),
            "spread_net_mean": (report.spread_net or {}).get("mean", np.nan),
            "cost_mean": report.cost_mean,
        },
        "meta": {
            "icir": icir,
            "ic_mean": ic_mean,
            "ic_pos": ic_pos,
            "year_ratio": year_ratio,
            "year_ci_ratio": year_ci_ratio,
            "regime_ratio": regime_ratio,
            "regime_ci_ratio": regime_ci_ratio,
            "rolling_sign_ratio": rolling_ratio,
            "monotonicity": mono,
            "coverage": report.coverage,
            "turnover": report.turnover,
            "spread_net_t": net_t,
            "incremental": incremental,
            "n_years": n_years,
            "min_sig_years": min_sig_years,
        },
        "notes": notes,
    }
