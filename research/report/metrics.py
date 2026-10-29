"""因子研究报告的指标计算层。

口径约定（与回测一致）
----------------------
- **可交易口径**：T 日收盘出信号，**T+1 开盘价买入**，持有 h 日后收盘卖出；
  即 ``fwd_h = close[t+h] / open[t+1] - 1``。绝不用 close-to-close，
  否则反转/涨停类因子会被系统性高估。
- **IC**：逐日横截面 Spearman（秩相关），不做全样本统计。
- **t 值**：Newey-West 调整（lag = h-1），处理重叠样本的自相关。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 242


# ---------------------------------------------------------------- 基础工具
def _spearman_ic(factor_row: pd.Series, ret_row: pd.Series) -> float:
    pair = pd.concat([factor_row.rename("f"), ret_row.rename("r")], axis=1).dropna()
    if len(pair) < 30 or pair["f"].nunique() < 5 or pair["r"].nunique() < 5:
        return np.nan
    return float(pair["f"].rank().corr(pair["r"].rank()))


def board_limit_pct(code: str) -> float:
    """按板块返回涨跌停幅度（ST 需外部标注，这里按代码前缀近似）。"""
    code = str(code)
    if code.startswith(("30", "68")):
        return 0.20          # 创业板 / 科创板
    if code.startswith(("43", "83", "87", "92")):
        return 0.30          # 北交所
    return 0.10              # 沪深主板


def tradeable_mask(
    close: pd.DataFrame, open_: pd.DataFrame, *, tolerance: float = 0.005
) -> pd.DataFrame:
    """``date × symbol``：t 日的信号，t+1 开盘能否买入。

    三条判定（任一不满足即不可成交）：

    1. t+1 有开盘价（停牌、退市为 NaN）；
    2. t+1 开盘价 **低于涨停价**（开盘一字涨停买不进）；
    3. 涨停价按板块规则由 t 日收盘价推出并四舍五入到分。
    """
    limits = pd.Series({c: board_limit_pct(c) for c in close.columns})
    limit_up = close.mul(1 + limits, axis=1).round(2)
    nxt_open = open_.shift(-1)
    return nxt_open.notna() & (nxt_open < limit_up - tolerance)


def forward_returns(
    close: pd.DataFrame, open_: pd.DataFrame, horizons: tuple[int, ...]
) -> dict[int, pd.DataFrame]:
    """T+1 开盘买入、T+h 收盘卖出的持有收益。"""
    entry = open_.shift(-1)
    return {h: close.shift(-h) / entry - 1 for h in horizons}


def newey_west_t(series: pd.Series, lag: int) -> float:
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    n = len(clean)
    if n < 10:
        return np.nan
    values = clean.to_numpy(dtype="float64")
    mean = values.mean()
    demeaned = values - mean
    var = (demeaned ** 2).sum() / n
    for k in range(1, min(lag, n - 1) + 1):
        cov = (demeaned[k:] * demeaned[:-k]).sum() / n
        var += 2 * (1 - k / (lag + 1)) * cov
    if var <= 0:
        return np.nan
    return float(mean / np.sqrt(var / n))


def _rank_ic_frame(
    factor: pd.DataFrame, forward: pd.DataFrame, *, min_names: int = 30
) -> pd.Series:
    """逐日 Spearman IC（向量化）。

    等价于逐日 `_spearman_ic`，但把 1650 次小循环换成整表 rank + 逐行 Pearson，
    实测快 20~40 倍；GPU（cuDF）在这里没有收益——瓶颈是 Python 循环，不是浮点吞吐。
    """
    dates = factor.index.intersection(forward.index)
    if len(dates) == 0:
        return pd.Series(dtype="float64")
    f = factor.reindex(dates)
    r = forward.reindex(dates)
    # 只保留两侧同时有效的样本，避免秩错位
    mask = f.notna() & r.notna()
    f = f.where(mask)
    r = r.where(mask)
    fr = f.rank(axis=1)
    rr = r.rank(axis=1)
    fm = fr.sub(fr.mean(axis=1), axis=0)
    rm = rr.sub(rr.mean(axis=1), axis=0)
    num = (fm * rm).sum(axis=1)
    den = np.sqrt((fm ** 2).sum(axis=1) * (rm ** 2).sum(axis=1)).replace(0, np.nan)
    ic = (num / den).replace([np.inf, -np.inf], np.nan)
    valid = mask.sum(axis=1) >= int(min_names)
    return ic.where(valid).dropna().sort_index()


def _quantile_nav(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    groups: int,
    horizon: int,
    cost_bps: float = 20.0,
    tradeable: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """分组等权组合：日度净值 + 每期收益。

    返回 ``(nav, turnover, period_returns)``。

    **为什么同时返回「每期收益」**：用复利终值比较分组优劣会被波动拖累（vol drag）
    和路径效应污染——高波动组即使期望相同，终值也会更低。分层单调性必须基于
    **每个持有期的平均收益**，而不是终值。
    """
    dates = factor.index
    members: dict[int, list[str]] = {}
    nav = pd.DataFrame(np.nan, index=dates, columns=[f"Q{i+1}" for i in range(groups)])
    turnover: dict[pd.Timestamp, float] = {}
    periods: list[dict[str, float]] = []
    prev_top: set[str] = set()
    level = {g: 1.0 for g in range(groups)}  # 跨换仓期累计，不能每期重置
    i = 0
    while i + horizon + 1 < len(dates):
        dt = dates[i]
        row = factor.loc[dt]
        valid = row.dropna()
        if tradeable is not None and dt in tradeable.index:
            ok = tradeable.loc[dt].reindex(valid.index).fillna(False)
            valid = valid[ok]
        if len(valid) >= groups * 5:
            ranked = valid.rank(method="first")
            buckets = pd.qcut(ranked, groups, labels=False, duplicates="drop")
            for g in range(groups):
                members[g] = list(valid.index[buckets == g])
            top = set(members[groups - 1])
            turnover[dt] = len(top - prev_top) / max(1, len(top))
            prev_top = top
            period_growth = {g: 1.0 for g in range(groups)}
            # 逐日路径（level 跨期延续）
            for j in range(i + 1, i + 1 + horizon):
                if j >= len(dates):
                    break
                for g in range(groups):
                    codes = [c for c in members[g] if c in close.columns]
                    if not codes:
                        continue
                    if j == i + 1:
                        px_in = open_.loc[dates[j], codes]
                        ret = (close.loc[dates[j], codes] / px_in - 1).mean()
                        ret -= cost_bps / 10000.0
                    else:
                        prev = close.loc[dates[j - 1], codes]
                        cur = close.loc[dates[j], codes]
                        ret = (cur / prev - 1).replace([np.inf, -np.inf], np.nan).mean()
                    growth = 1 + (ret if ret == ret else 0.0)
                    level[g] *= growth
                    period_growth[g] *= growth
                    nav.loc[dates[j], f"Q{g+1}"] = level[g]
            periods.append({f"Q{g+1}": period_growth[g] - 1 for g in range(groups)})
        i += horizon
    return nav.ffill(), pd.Series(turnover).sort_index(), pd.DataFrame(periods)


# ---------------------------------------------------------------- 报告数据
@dataclass
class FactorReport:
    factor_id: str
    category: str
    direction: int
    universe: str
    start: pd.Timestamp
    end: pd.Timestamp
    n_symbols: int
    n_dates: int
    coverage: float
    horizons: tuple[int, ...]

    ic: dict[int, pd.Series] = field(default_factory=dict)
    ic_stats: pd.DataFrame = field(default_factory=pd.DataFrame)
    rolling_ic: pd.Series | None = None
    ic_by_year: pd.Series | None = None
    ic_by_regime: pd.Series | None = None
    decay: pd.Series | None = None

    nav: pd.DataFrame | None = None
    period_returns: pd.DataFrame | None = None
    quantile_returns: dict[str, float] = field(default_factory=dict)
    monotonicity: float = float("nan")
    long_short: dict[str, float] = field(default_factory=dict)
    turnover: float = float("nan")
    ic_by_size: pd.Series | None = None
    ic_by_industry: pd.Series | None = None
    autocorr: float = float("nan")
    excess_ic: pd.Series | None = None

    limit_up: dict = field(default_factory=dict)
    rating: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def build_report(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    factor_id: str,
    category: str = "momentum",
    direction: int = 1,
    universe: str = "csi800",
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    primary_horizon: int = 5,
    groups: int = 5,
    cost_bps: float = 20.0,
    regime: pd.Series | None = None,
    market_cap: pd.DataFrame | None = None,
    industry: pd.DataFrame | None = None,
    exclude_limit_up: bool = False,
) -> FactorReport:
    fwd = forward_returns(close, open_, horizons)
    mask = tradeable_mask(close, open_)
    excluded_ratio = float((~mask).to_numpy().mean())

    report = FactorReport(
        factor_id=factor_id,
        category=category,
        direction=direction,
        universe=universe,
        start=factor.index.min(),
        end=factor.index.max(),
        n_symbols=int(factor.shape[1]),
        n_dates=int(factor.shape[0]),
        coverage=float(factor.notna().sum().sum() / max(1, factor.size)),
        horizons=horizons,
    )

    analysis_factor = factor * (1 if direction >= 0 else -1)
    # 可成交过滤：t+1 开盘一字涨停/停牌的样本不可买入，IC 与分层都必须剔除
    fwd_raw = dict(fwd)
    if exclude_limit_up:
        fwd = {h: v.where(mask) for h, v in fwd.items()}
    report.limit_up = {
        "applied": bool(exclude_limit_up),
        "excluded_ratio": excluded_ratio,
        "ic_raw": float(_rank_ic_frame(analysis_factor, fwd_raw[primary_horizon]).mean()),
        "ic_masked": float(_rank_ic_frame(analysis_factor, fwd[primary_horizon]).mean()),
    }
    # 方向自适应：设计方向的 IC 若为负，说明信号反向有效——翻转后重算，
    # 并在报告里显式标注，避免“负 IC 因子拿到高评级”。
    probe = _rank_ic_frame(analysis_factor, fwd[primary_horizon])
    flipped = bool(len(probe) > 20 and probe.mean() < 0)
    if flipped:
        analysis_factor = -analysis_factor
        report.warnings.append(
            f"原始方向 IC 为负（{probe.mean():+.3f}），已按反向信号评估；"
            "若经济逻辑不支持反向使用，应判定为无效因子"
        )
        report.direction = -1
    else:
        report.direction = 1 if direction >= 0 else -1

    rows = []
    for h in horizons:
        series = _rank_ic_frame(analysis_factor, fwd[h])
        report.ic[h] = series
        if series.empty:
            continue
        icir = series.mean() / (series.std() or np.nan) * np.sqrt(TRADING_DAYS / h)
        rows.append(
            {
                "h": h,
                "mean": series.mean(),
                "std": series.std(),
                "ir": icir,
                "t": newey_west_t(series, lag=max(1, h - 1)),
                "win": float((series > 0).mean()),
                "n": int(len(series)),
            }
        )
    report.ic_stats = pd.DataFrame(rows).set_index("h") if rows else pd.DataFrame()

    primary = report.ic.get(primary_horizon, pd.Series(dtype="float64"))
    if not primary.empty:
        window = max(20, min(60, len(primary) // 6 or 20))
        report.rolling_ic = primary.rolling(window, min_periods=max(5, window // 3)).mean()
        report.ic_by_year = primary.groupby(primary.index.year).mean()
        if regime is not None:
            aligned = regime.reindex(primary.index).dropna()
            if not aligned.empty:
                report.ic_by_regime = primary.reindex(aligned.index).groupby(aligned).mean()
        report.decay = pd.Series({h: s.mean() for h, s in report.ic.items()})

    nav, turnover_series, period_returns = _quantile_nav(
        analysis_factor, close, open_, groups=groups, horizon=primary_horizon, cost_bps=cost_bps,
        tradeable=mask if exclude_limit_up else None,
    )
    report.nav = nav
    report.period_returns = period_returns
    if not period_returns.empty:
        # 分层优劣用「每期平均收益」，不用复利终值（避免波动拖累污染单调性）
        means = period_returns.mean()
        report.quantile_returns = {str(k): float(v) for k, v in means.items()}
        if len(means) >= 3:
            report.monotonicity = float(
                pd.Series(np.arange(len(means)), index=means.index).corr(means.rank())
            )
        spread = period_returns.get(f"Q{groups}", pd.Series(dtype="float")) - period_returns.get(
            "Q1", pd.Series(dtype="float")
        )
        if len(spread.dropna()) > 10:
            mean_period = float(spread.mean())
            std_period = float(spread.std())
            report.long_short = {
                "total": float(mean_period * len(spread)),
                "ann": float(mean_period * (TRADING_DAYS / primary_horizon)),
                "sharpe": float(mean_period / std_period * np.sqrt(TRADING_DAYS / primary_horizon))
                if std_period
                else float("nan"),
                "mdd": float((spread.cumsum() - spread.cumsum().cummax()).min()),
            }
    report.turnover = float(turnover_series.mean()) if not turnover_series.empty else float("nan")

    if market_cap is not None:
        ic_by_size = {}
        caps = market_cap.reindex(index=factor.index, columns=factor.columns)
        for label, lo, hi in (("小市值", 0.0, 1 / 3), ("中市值", 1 / 3, 2 / 3), ("大市值", 2 / 3, 1.0)):
            mask = caps.rank(axis=1, pct=True)
            masked = analysis_factor.where((mask >= lo) & (mask <= hi))
            series = _rank_ic_frame(masked, fwd[primary_horizon])
            if len(series) > 20:
                ic_by_size[label] = float(series.mean())
        report.ic_by_size = pd.Series(ic_by_size) if ic_by_size else None

    if industry is not None:
        industry = industry.reindex(index=factor.index, columns=factor.columns)
        ic_by_ind = {}
        for name in pd.unique(industry.iloc[-1].dropna()):
            mask = industry == name
            masked = analysis_factor.where(mask)
            series = _rank_ic_frame(masked, fwd[primary_horizon])
            if len(series) > 20:
                ic_by_ind[str(name)] = float(series.mean())
        if ic_by_ind:
            report.ic_by_industry = pd.Series(ic_by_ind).sort_values(ascending=False)

    ranks = analysis_factor.rank(axis=1, pct=True)
    autocorrs = []
    for k in range(1, 21):
        a, b = ranks.iloc[k:], ranks.iloc[:-k]
        pair = pd.concat([a.stack(), b.stack()], axis=1).dropna()
        if len(pair) > 1000:
            autocorrs.append(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
    report.autocorr = float(np.nanmean(autocorrs)) if autocorrs else float("nan")

    if report.ic_by_regime is not None and len(report.ic_by_regime) > 1:
        if report.ic_by_regime.min() < 0 < report.ic_by_regime.max():
            report.warnings.append(
                "因子在部分市场状态下 IC 为负（"
                + "、".join(f"{k}:{v:+.3f}" for k, v in report.ic_by_regime.items())
                + "），存在状态依赖"
            )
    if report.coverage < 0.7:
        report.warnings.append(f"覆盖率仅 {report.coverage:.0%}（< 70%），横截面代表性不足")
    if report.turnover == report.turnover and report.turnover > 0.6:
        report.warnings.append(f"Top 组换手 {report.turnover:.0%} 偏高，成本敏感")
    return report
