"""因子评级：100 分制评分卡 + 一票否决 + 二维定位。"""

from __future__ import annotations

import math

import numpy as np


def _band(value: float, bands: list[tuple[float, float]], default: float) -> float:
    """``bands`` 为 (阈值, 得分)，按阈值从高到低匹配。"""
    if value != value:
        return default
    for threshold, score in bands:
        if value >= threshold:
            return score
    return bands[-1][1] if bands else default


def rate(report) -> dict:
    stats = report.ic_stats
    primary_h = 5 if 5 in stats.index else (stats.index[0] if len(stats) else None)

    icir = float(stats.loc[primary_h, "ir"]) if primary_h is not None else float("nan")
    ic_mean = float(stats.loc[primary_h, "mean"]) if primary_h is not None else float("nan")

    # ---- 预测力 30：IC 幅度 15 + ICIR 15
    # 只看 ICIR 会把「IC 很小但很稳」的因子误判为强因子，必须同时看幅度。
    ic_mag = abs(ic_mean) if ic_mean == ic_mean else 0.0
    mag_score = _band(ic_mag, [(0.05, 15.0), (0.03, 11.0), (0.02, 6.0)], 2.0)
    ir_score = _band(abs(icir), [(0.50, 15.0), (0.35, 11.0), (0.20, 6.0)], 2.0)
    power = mag_score + ir_score

    # ---- 时间稳定性 25：分年同号 + 分状态同号
    year_ratio = float("nan")
    if report.ic_by_year is not None and len(report.ic_by_year) >= 2:
        signs = np.sign(report.ic_by_year.dropna().to_numpy())
        year_ratio = float(np.mean(signs > 0)) if ic_mean >= 0 else float(np.mean(signs < 0))
    regime_ratio = float("nan")
    if report.ic_by_regime is not None and len(report.ic_by_regime.dropna()) >= 2:
        vals = report.ic_by_regime.dropna().to_numpy()
        regime_ratio = float(np.mean(vals > 0)) if ic_mean >= 0 else float(np.mean(vals < 0))

    stability = 0.0
    if year_ratio == year_ratio:
        stability += 12.5 * year_ratio
    if regime_ratio == regime_ratio:
        stability += 12.5 * regime_ratio

    # ---- 空间单调性 15（必须是有符号单调性：方向已对齐后仍为负，说明分层自相矛盾）
    mono = report.monotonicity if report.monotonicity == report.monotonicity else 0.0
    if mono >= 0.80:
        monotonicity = 15.0
    elif mono >= 0.60:
        monotonicity = 10.0
    elif mono >= 0.40:
        monotonicity = 6.0
    elif mono >= 0.0:
        monotonicity = 3.0
    else:
        monotonicity = 0.0

    # ---- 可交易性 15（覆盖 5 + 换手 5 + 多空夏普 5）
    coverage = _band(report.coverage, [(0.85, 5.0), (0.70, 3.0)], 1.0)
    turnover = _band(
        -(report.turnover if report.turnover == report.turnover else 1.0),
        [(-0.30, 5.0), (-0.50, 3.0)],
        1.0,
    )
    sharpe = report.long_short.get("sharpe", float("nan"))
    ls_score = _band(sharpe, [(1.0, 5.0), (0.5, 3.0), (0.0, 2.0)], 0.0)

    # ---- 增量性 15：暂以「与同类因子相关性」代理，无数据时给中性分
    incremental = 8.0
    incremental_note = "未接入同类因子相关性，暂给中性分 8/15"

    items = [
        ("预测力（ICIR）", power, 30.0),
        ("时间稳定性", stability, 25.0),
        ("空间单调性", monotonicity, 15.0),
        ("可交易性", coverage + turnover + ls_score, 15.0),
        ("增量性", incremental, 15.0),
    ]
    total = sum(score for _, score, _ in items)

    grade = "D"
    if total >= 80:
        grade = "A"
    elif total >= 65:
        grade = "B"
    elif total >= 50:
        grade = "C"

    vetoes: list[str] = []
    if abs(ic_mean) < 0.02:
        vetoes.append(f"IC 幅度仅 {abs(ic_mean):.3f} < 0.02，预测力不足")
    if report.coverage < 0.70:
        vetoes.append(f"覆盖率 {report.coverage:.0%} < 70%")
    if year_ratio == year_ratio and year_ratio < 0.6:
        vetoes.append(f"分年同号率 {year_ratio:.0%} < 60%")
    spread = float("nan")
    if report.quantile_returns and len(report.quantile_returns) >= 2:
        vals = list(report.quantile_returns.values())
        spread = abs(vals[-1] - vals[0])
        if spread < 0.002:
            vetoes.append(
                f"多空价差 {spread*100:.2f}% < 单期成本 0.20%，分层收益不具备可交易性"
            )
    if mono < 0:
        vetoes.append(f"分层单调性为负（{mono:.2f}），分层与 IC 方向矛盾")
    if report.turnover == report.turnover and report.turnover > 0.80:
        vetoes.append(f"Top 组换手 {report.turnover:.0%} > 80%")
    if vetoes and grade in ("A", "B"):
        grade = "C"

    time_score = 100.0 * ((year_ratio if year_ratio == year_ratio else 0.5) * 0.6
                          + (regime_ratio if regime_ratio == regime_ratio else 0.5) * 0.4)
    space_score = 100.0 * max(0.0, min(1.0, 0.7 * mono + 0.3 * min(1.0, ic_mag / 0.06)))

    if time_score >= 60 and space_score >= 60:
        quadrant = ("核心因子", "可直接进多因子合成，权重 15%~25%")
    elif time_score < 60 and space_score >= 60:
        quadrant = ("择时因子", "空间区分度强但状态依赖，只在特定 regime 启用")
    elif time_score >= 60 and space_score < 60:
        quadrant = ("过滤因子", "适合做负面剔除/风控条件，不做打分")
    else:
        quadrant = ("淘汰", "时间与空间均不达标，归档并记录失效原因")

    return {
        "score": round(total, 1),
        "grade": grade,
        "items": items,
        "vetoes": vetoes,
        "time_score": round(time_score, 1),
        "space_score": round(space_score, 1),
        "quadrant": quadrant[0],
        "usage": quadrant[1],
        "meta": {
            "icir": icir,
            "ic_mean": ic_mean,
            "year_ratio": year_ratio,
            "regime_ratio": regime_ratio,
            "monotonicity": mono,
            "coverage": report.coverage,
            "turnover": report.turnover,
            "long_short_sharpe": sharpe,
        },
        "notes": [incremental_note],
    }
