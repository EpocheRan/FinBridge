"""因子研究报告的指标计算层。

口径约定（与回测一致）
---------------------
- **可交易口径**：T 日收盘出信号，**T+1 开盘价买入**，持有 h 日后收盘卖出；
  即 ``fwd_h = close[t+h] / open[t+1] - 1``。绝不用 close-to-close，
  否则反转/涨停类因子会被系统性高估。
- **IC**：逐日横截面 Spearman（秩相关），不做全样本统计。
- **t 值**：Newey-West 调整（lag = h-1）处理重叠样本；同时给出**非重叠**
  子样本 t 值（每 h 个交易日取一个观测），避免 NW 调整在小样本下过度自信。
- **中性化**：横截面回归剔除对数市值与行业哑变量后的残差 IC。
- **净值**：分组等权、非重叠调仓、T+1 开盘买入；成本按换手率×双边费率扣除。

本模块只做计算，不读数据库、不渲染。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import diagnostics as diag

TRADING_DAYS = 242


# ---------------------------------------------------------------- 基础工具
def _spearman_ic(factor_row: pd.Series, ret_row: pd.Series) -> float:
    pair = pd.concat([factor_row.rename("f"), ret_row.rename("r")], axis=1).dropna()
    if len(pair) < 30 or pair["f"].nunique() < 5 or pair["r"].nunique() < 5:
        return np.nan
    return float(pair["f"].rank().corr(pair["r"].rank()))


def board_limit_pct(code: str, *, is_st: bool = False) -> float:
    """按板块返回涨跌停幅度。

    ST 股票为 5%（仅能拿到**当前** ST 标记，历史 ST 状态未知，见报告局限）；
    创业板/科创板 20%；北交所 30%；其余主板 10%。
    """
    code = str(code)
    if is_st:
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("43", "83", "87", "92")):
        return 0.30
    return 0.10


def _limit_matrix(close: pd.DataFrame, is_st: dict[str, bool] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """由 t 日收盘推出 t+1 的涨停价与跌停价（四舍五入到分）。"""
    limits = pd.Series(
        {c: board_limit_pct(c, is_st=bool((is_st or {}).get(c, False))) for c in close.columns}
    )
    return close.mul(1 + limits, axis=1).round(2), close.mul(1 - limits, axis=1).round(2)


def tradeable_mask(
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    tolerance: float = 0.005,
    is_st: dict[str, bool] | None = None,
) -> pd.DataFrame:
    """``date × symbol``：t 日的信号，t+1 开盘能否买入。

    三条判定（任一不满足即不可成交）：

    1. t+1 有开盘价（停牌、退市为 NaN）；
    2. t+1 开盘价 **低于涨停价**（开盘一字涨停买不进）；
    3. 涨停价按板块规则由 t 日收盘价推出并四舍五入到分。
    """
    limit_up, _ = _limit_matrix(close, is_st)
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


def independent_t(series: pd.Series, step: int) -> tuple[float, int]:
    """非重叠子样本 t 值：每 ``step`` 个交易日取一个观测。

    重叠样本（h>1）的 NW-t 对滞后阶数敏感，且在小样本下容易高估显著性；
    报告同时给出独立子样本 t，作为「保守下界」。
    """
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    if step < 1 or len(clean) < step * 10:
        return np.nan, 0
    sampled = clean.iloc[::step]
    n = len(sampled)
    std = sampled.std(ddof=1)
    if not np.isfinite(std) or std == 0:
        return np.nan, n
    return float(sampled.mean() / (std / np.sqrt(n))), n


def block_bootstrap_ci(
    series: pd.Series,
    *,
    block: int = 5,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[float, float]:
    """循环块自助法置信区间（保留重叠样本的自相关结构）。"""
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    values = clean.to_numpy(dtype="float64")
    n = len(values)
    if n < max(30, block * 4):
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    idx = idx.reshape(n_boot, -1)[:, :n]
    means = values[idx].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


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


# ---------------------------------------------------------------- 中性化
def _winsorize(values: np.ndarray, lower: float = 0.01, upper: float = 0.99) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if len(finite) < 20:
        return values
    lo, hi = np.quantile(finite, [lower, upper])
    return np.clip(values, lo, hi)


def _zscore(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    if finite.sum() < 2:
        return values
    mean = np.nanmean(values[finite])
    std = np.nanstd(values[finite])
    if not np.isfinite(std) or std == 0:
        return values
    out = (values - mean) / std
    out[~finite] = np.nan
    return out


def neutralize_panel(
    factor: pd.DataFrame,
    market_cap: pd.DataFrame | None,
    industry: pd.DataFrame | None,
    *,
    mode: str = "both",
) -> pd.DataFrame:
    """横截面中性化：对 log 市值与行业哑变量回归，返回残差面板。

    ``mode``：``size`` / ``industry`` / ``both``。
    行业用**当日**的静态映射（见报告局限）；先缩尾再标准化，避免极值主导回归。
    """
    values = factor.to_numpy(dtype="float64")
    n_rows, n_cols = values.shape
    cap = (
        market_cap.reindex(index=factor.index, columns=factor.columns).to_numpy(dtype="float64")
        if market_cap is not None
        else np.full_like(values, np.nan)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        log_cap = np.where(cap > 0, np.log(cap), np.nan)

    codes = np.full(n_cols, -1, dtype="int64")
    if industry is not None and mode in ("industry", "both"):
        last = industry.reindex(columns=factor.columns).ffill().iloc[-1]
        mapping = {name: i for i, name in enumerate(pd.unique(last.dropna()))}
        codes = np.array([mapping.get(v, -1) if pd.notna(v) else -1 for v in last.to_numpy()], dtype="int64")

    out = np.full_like(values, np.nan)
    for i in range(n_rows):
        y = _winsorize(values[i])
        valid = np.isfinite(y)
        if valid.sum() < 30:
            continue
        y = _zscore(y)
        resid = y.copy()

        if mode in ("industry", "both") and codes.max() >= 0:
            group = codes
            valid_g = valid & (group >= 0)
            if valid_g.sum() >= 30:
                counts = np.bincount(group[valid_g], minlength=int(group.max()) + 1)
                sums = np.bincount(group[valid_g], weights=y[valid_g], minlength=len(counts))
                means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
                resid[valid_g] = y[valid_g] - means[group[valid_g]]

        if mode in ("size", "both"):
            cap_row = _zscore(log_cap[i])
            both = np.isfinite(resid) & np.isfinite(cap_row)
            if both.sum() >= 30:
                x = cap_row[both]
                r = resid[both]
                # 行业中性化后，cap 也要在行业内去均值，否则行业与市值共线
                if mode == "both" and codes.max() >= 0:
                    group = codes
                    ok_g = both & (group >= 0)
                    if ok_g.sum() >= 30:
                        counts = np.bincount(group[ok_g], minlength=int(group.max()) + 1)
                        sums = np.bincount(
                            group[ok_g], weights=cap_row[ok_g], minlength=len(counts)
                        )
                        means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
                        cap_neutral = cap_row.copy()
                        cap_neutral[ok_g] = cap_row[ok_g] - means[group[ok_g]]
                        x = cap_neutral[both]
                denom = float((x * x).sum())
                if denom > 0:
                    beta = float((r * x).sum() / denom)
                    resid[both] = r - beta * x
        out[i] = resid
    return pd.DataFrame(out, index=factor.index, columns=factor.columns)


def neutralized_ic(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    market_cap: pd.DataFrame | None,
    industry: pd.DataFrame | None,
) -> pd.DataFrame:
    """原始 / 市值中性 / 行业中性 / 双中性 的 IC 对比。"""
    rows = {}
    variants = {
        "原始": None,
        "市值中性": "size" if market_cap is not None else None,
        "行业中性": "industry" if industry is not None else None,
        "双中性": "both" if (market_cap is not None and industry is not None) else None,
    }
    for label, mode in variants.items():
        if label != "原始" and mode is None:
            continue
        panel = factor if mode is None else neutralize_panel(factor, market_cap, industry, mode=mode)
        series = _rank_ic_frame(panel, forward)
        if series.empty:
            continue
        rows[label] = {
            "mean": float(series.mean()),
            "std": float(series.std()),
            "ir": float(series.mean() / series.std() * np.sqrt(TRADING_DAYS / 5)) if series.std() else np.nan,
            "t": newey_west_t(series, lag=4),
            "n": int(len(series)),
        }
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------- 组合净值
def _quantile_nav(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    groups: int,
    horizon: int,
    cost_bps: float = 20.0,
    tradeable: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """分组等权组合：日度净值 + 每期收益（毛/净）+ 日收益 + 换手。

    成本口径：``cost_bps`` 为**单腿往返**费率（买+卖），按该组换手率摊在调仓日：
    ``cost = turnover_g × cost_bps``。多空组合两条腿各计一次：
    ``(turnover_Q5 + turnover_Q1) × cost_bps``。

    返回 ``(nav, turnover_df, periods_net, periods_gross, daily_gross, ls_cost, turnover_mean)``。
    """
    dates = factor.index
    labels = [f"Q{i + 1}" for i in range(groups)]
    nav = pd.DataFrame(np.nan, index=dates, columns=labels)
    daily = pd.DataFrame(0.0, index=dates, columns=labels)
    ls_cost = pd.Series(0.0, index=dates)
    periods_net: list[dict[str, float]] = []
    periods_gross: list[dict[str, float]] = []
    period_dates: list[pd.Timestamp] = []
    turnover_rows: list[dict[str, float]] = []
    prev_members: dict[int, set[str]] = {g: set() for g in range(groups)}
    level = {g: 1.0 for g in range(groups)}
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
            members = {g: list(valid.index[buckets == g]) for g in range(groups)}
            turnover = {}
            for g in range(groups):
                current = set(members[g])
                turnover[g] = len(current - prev_members[g]) / max(1, len(current))
                prev_members[g] = current
            turnover_rows.append({f"Q{g + 1}": turnover[g] for g in range(groups)})

            period_gross = {g: 1.0 for g in range(groups)}
            period_net = {g: 1.0 for g in range(groups)}
            for j in range(i + 1, i + 1 + horizon):
                if j >= len(dates):
                    break
                for g in range(groups):
                    codes = [c for c in members[g] if c in close.columns]
                    if not codes:
                        continue
                    if j == i + 1:
                        px_in = open_.loc[dates[j], codes]
                        ret = float((close.loc[dates[j], codes] / px_in - 1).mean())
                        cost = turnover[g] * cost_bps / 10000.0
                        net = ret - cost
                    else:
                        prev = close.loc[dates[j - 1], codes]
                        cur = close.loc[dates[j], codes]
                        ret = float((cur / prev - 1).replace([np.inf, -np.inf], np.nan).mean())
                        net = ret
                    if not np.isfinite(ret):
                        ret, net = 0.0, 0.0
                    period_gross[g] *= 1 + ret
                    period_net[g] *= 1 + net
                    level[g] *= 1 + net
                    daily.loc[dates[j], f"Q{g + 1}"] = ret
                    nav.loc[dates[j], f"Q{g + 1}"] = level[g]
                if j == i + 1:
                    ls_cost.loc[dates[j]] = (
                        (turnover[groups - 1] + turnover[0]) * cost_bps / 10000.0
                    )
            periods_net.append({f"Q{g + 1}": period_net[g] - 1 for g in range(groups)})
            periods_gross.append({f"Q{g + 1}": period_gross[g] - 1 for g in range(groups)})
            period_dates.append(dt)
        i += horizon
    turnover_df = pd.DataFrame(turnover_rows, index=period_dates)
    return (
        nav.ffill(), turnover_df,
        pd.DataFrame(periods_net, index=period_dates),
        pd.DataFrame(periods_gross, index=period_dates),
        daily, ls_cost, turnover_df.mean(),
    )


def _performance(nav: pd.Series) -> dict:
    clean = nav.dropna()
    if len(clean) < 2:
        return {}
    daily = clean.pct_change().dropna()
    total = float(clean.iloc[-1] / clean.iloc[0] - 1)
    years = len(clean) / TRADING_DAYS
    ann = float((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else np.nan
    vol = float(daily.std() * np.sqrt(TRADING_DAYS))
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if daily.std() else np.nan
    drawdown = clean / clean.cummax() - 1
    mdd = float(drawdown.min())
    calmar = float(ann / abs(mdd)) if mdd < 0 and np.isfinite(ann) else np.nan
    return {
        "total": total,
        "ann": ann,
        "vol": vol,
        "sharpe": sharpe,
        "mdd": mdd,
        "calmar": calmar,
        "win": float((daily > 0).mean()),
        "n_days": int(len(clean)),
    }


def _period_performance(period_returns: pd.Series, horizon: int) -> dict:
    """非重叠持有期组合的绩效（与分层表同一策略）。"""
    clean = period_returns.dropna()
    if len(clean) < 2:
        return {}
    nav = (1 + clean).cumprod()
    total = float(nav.iloc[-1] - 1)
    ann = float(nav.iloc[-1] ** (TRADING_DAYS / (len(clean) * horizon)) - 1)
    vol = float(clean.std() * np.sqrt(TRADING_DAYS / horizon))
    sharpe = float(clean.mean() / clean.std() * np.sqrt(TRADING_DAYS / horizon)) if clean.std() else np.nan
    drawdown = nav / nav.cummax() - 1
    mdd = float(drawdown.min())
    calmar = float(ann / abs(mdd)) if mdd < 0 else np.nan
    return {
        "total": total,
        "ann": ann,
        "vol": vol,
        "sharpe": sharpe,
        "mdd": mdd,
        "calmar": calmar,
        "win": float((clean > 0).mean()),
        "n_days": int(len(clean)),
    }


def _annual_returns(nav: pd.Series) -> pd.Series:
    clean = nav.dropna()
    if clean.empty:
        return pd.Series(dtype="float64")
    yearly = clean.groupby(clean.index.year).last()
    prev = yearly.shift(1)
    first = clean.iloc[0]
    out = {}
    for year, value in yearly.items():
        base = prev.get(year, np.nan)
        if not np.isfinite(base):
            base = first if year == yearly.index[0] else np.nan
        out[int(year)] = float(value / base - 1) if base and np.isfinite(base) else np.nan
    return pd.Series(out)


def _delayed_exit(
    close: pd.DataFrame,
    open_: pd.DataFrame,
    horizon: int,
    *,
    max_delay: int = 10,
    is_st: dict[str, bool] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """卖出端可执行性：T+h 收盘跌停/停牌时顺延到下一个可卖日。

    返回 ``(delayed_returns, delay_days, blocked_at_target)``。
    """
    dates = close.index
    n = len(dates)
    _, limit_down = _limit_matrix(close, is_st)
    limit_down = limit_down.shift(1)  # 当日跌停价由前一交易日收盘推出
    values = close.to_numpy(dtype="float64")
    open_values = open_.to_numpy(dtype="float64")
    ld = limit_down.to_numpy(dtype="float64")
    entry = np.vstack([open_values[1:], np.full((1, close.shape[1]), np.nan)])
    target = np.arange(n) + horizon
    target = np.minimum(target, n - 1)
    col_idx = np.arange(close.shape[1])

    def _blocked_at(idx: np.ndarray) -> np.ndarray:
        price = values[idx, col_idx[None, :]]
        floor = ld[idx, col_idx[None, :]]
        return (~np.isfinite(price)) | (price <= floor + 0.005)

    exit_idx = np.broadcast_to(target[:, None], (n, close.shape[1])).copy()
    blocked0 = _blocked_at(exit_idx)
    blocked = blocked0.copy()
    for _ in range(max_delay):
        bad = blocked & (exit_idx < n - 1)
        if not bad.any():
            break
        exit_idx = np.where(bad, exit_idx + 1, exit_idx)
        blocked = _blocked_at(exit_idx)
    exit_close = values[exit_idx, col_idx[None, :]]
    ret = exit_close / entry - 1
    ret[~np.isfinite(entry)] = np.nan
    delay = np.where(np.isfinite(entry), exit_idx - target[:, None], np.nan)
    # 最后一行没有 T+1 开盘，置空
    ret[-1, :] = np.nan
    delay[-1, :] = np.nan
    return (
        pd.DataFrame(ret, index=dates, columns=close.columns),
        pd.DataFrame(delay, index=dates, columns=close.columns),
        pd.DataFrame(blocked0, index=dates, columns=close.columns),
    )


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
    ic_by_year_ci: pd.DataFrame | None = None
    ic_by_regime: pd.Series | None = None
    regime_stats: pd.DataFrame | None = None
    regime_stats_alt: pd.DataFrame | None = None
    regime_share: pd.DataFrame | None = None
    regime_corr: float = float("nan")
    regime_corr_p: float = float("nan")
    decay: pd.Series | None = None
    decay_note: str = ""

    nav: pd.DataFrame | None = None
    period_returns: pd.DataFrame | None = None
    period_returns_gross: pd.DataFrame | None = None
    period_returns_net: pd.DataFrame | None = None
    daily_returns: pd.DataFrame | None = None
    quantile_returns: dict[str, float] = field(default_factory=dict)
    quantile_returns_gross: dict[str, float] = field(default_factory=dict)
    quantile_ci: dict[str, tuple[float, float]] = field(default_factory=dict)
    monotonicity: float = float("nan")
    monotonicity_net: float = float("nan")
    monotonicity_p: float = float("nan")
    spread_gross: dict = field(default_factory=dict)
    spread_net: dict = field(default_factory=dict)
    cost_mean: float = float("nan")
    sigma_5d: float = float("nan")
    transmission: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    attribution: dict = field(default_factory=dict)
    direction_source: str = "registry"
    ic_half: dict = field(default_factory=dict)
    reproduction: dict = field(default_factory=dict)
    long_short: dict[str, float] = field(default_factory=dict)
    ls_nav: pd.Series | None = None
    ls_daily: pd.Series | None = None
    drawdown: pd.Series | None = None
    perf: dict = field(default_factory=dict)
    annual_returns: pd.Series | None = None
    benchmark_nav: pd.Series | None = None
    turnover: float = float("nan")
    turnover_by_group: pd.Series | None = None

    filter_test: dict = field(default_factory=dict)
    annual_ic_return: pd.DataFrame | None = None

    ic_neutral: pd.DataFrame | None = None
    ic_by_size: pd.Series | None = None
    ic_by_industry: pd.Series | None = None
    industry_neutral_ic: float = float("nan")
    autocorr: float = float("nan")

    limit_up: dict = field(default_factory=dict)
    tradeability: dict = field(default_factory=dict)
    sell_side: dict = field(default_factory=dict)
    cost_table: pd.DataFrame | None = None
    capacity: dict = field(default_factory=dict)
    incrementality: dict = field(default_factory=dict)
    peers: pd.Series | None = None

    definition: dict = field(default_factory=dict)
    factor_stats: dict = field(default_factory=dict)
    pool: dict = field(default_factory=dict)
    pool_robustness: pd.DataFrame | None = None
    freshness: dict = field(default_factory=dict)
    raw_direction: int = 1
    flipped: bool = False
    rating: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _regime_table(ic: pd.Series, regime: pd.Series, *, block: int = 5) -> pd.DataFrame:
    rows = {}
    aligned = regime.reindex(ic.index)
    for key, label in (("bull", "牛市"), ("neutral", "震荡"), ("bear", "熊市")):
        sub = ic[aligned == key]
        if len(sub) < 20:
            continue
        lo, hi = block_bootstrap_ci(sub, block=block)
        rows[label] = {
            "mean": float(sub.mean()),
            "n": int(len(sub)),
            "ci_lo": lo,
            "ci_hi": hi,
            "t": newey_west_t(sub, lag=4),
        }
    if {"牛市", "熊市"}.issubset(rows):
        bull = ic[aligned == "bull"]
        bear = ic[aligned == "bear"]
        diff = float(bull.mean() - bear.mean())
        rng = np.random.default_rng(11)
        boot = []
        for _ in range(2000):
            b = bull.to_numpy()[rng.integers(0, len(bull), len(bull))].mean()
            r = bear.to_numpy()[rng.integers(0, len(bear), len(bear))].mean()
            boot.append(b - r)
        boot = np.asarray(boot)
        rows["牛市−熊市"] = {
            "mean": diff,
            "n": int(len(bull) + len(bear)),
            "ci_lo": float(np.quantile(boot, 0.025)),
            "ci_hi": float(np.quantile(boot, 0.975)),
            "t": float("nan"),
            "p": float(2 * min((boot <= 0).mean(), (boot >= 0).mean())),
        }
    return pd.DataFrame(rows).T


def _decay_note(decay: pd.Series) -> str:
    clean = decay.dropna()
    if len(clean) < 2:
        return "持有期样本不足，无法判断衰减形态。"
    hs = list(clean.index)
    vals = clean.to_numpy()
    peak_h = hs[int(np.argmax(vals))]
    peak = float(vals.max())
    first = float(vals[0])
    if peak_h == max(hs) and peak > first * 1.2:
        return (
            f"IC 随持有期上升，在 h={peak_h} 达到最高 {peak:+.3f}，"
            "未见衰减；5 日并非最优持有期，延长持有期可提高信号强度，"
            "但需同时评估样本重叠、容量与调仓成本。"
        )
    if abs(peak) < 0.005:
        return "各持有期 IC 均接近 0，衰减形态无经济含义。"
    # 半衰期：IC 降到峰值一半所需的持有期（线性插值）
    half = peak / 2
    half_life = None
    for a, b, va, vb in zip(hs, hs[1:], vals, vals[1:]):
        if (va - half) * (vb - half) <= 0 and va != vb:
            half_life = a + (b - a) * (va - half) / (va - vb)
            break
    if half_life is not None:
        return (
            f"IC 在 h={peak_h} 见顶（{peak:+.3f}），半衰期约 {half_life:.1f} 个交易日；"
            "若目标持有期显著长于半衰期，应缩短换仓周期或降低单期权重。"
        )
    return f"IC 在 h={peak_h} 见顶（{peak:+.3f}），随后走弱；衰减速度见曲线。"


def _capacity(amount: pd.DataFrame, factor: pd.DataFrame, horizon: int, groups: int) -> dict:
    """容量估算：Top 组等权，单票不超过 ADV 的 10%，3 日建仓。"""
    if amount is None or amount.empty or factor.empty:
        return {}
    dates = factor.index
    adv = amount.rolling(20, min_periods=5).mean()
    caps = []
    last_rows = []
    i = 0
    while i + horizon + 1 < len(dates):
        dt = dates[i]
        row = factor.loc[dt].dropna()
        if len(row) >= groups * 5:
            ranked = row.rank(method="first")
            buckets = pd.qcut(ranked, groups, labels=False, duplicates="drop")
            members = list(row.index[buckets == groups - 1])
            if members and dt in adv.index:
                values = adv.loc[dt, members].dropna()
                if len(values) >= max(5, len(members) // 2):
                    per_name = values * 0.1 * 3  # 10% ADV × 3 天
                    caps.append(float(np.quantile(per_name, 0.1) * len(members)))
                    last_rows.append(values)
        i += horizon
    if not caps:
        return {}
    latest = last_rows[-1] if last_rows else pd.Series(dtype="float64")
    return {
        "aum_est": float(np.median(caps)),
        "aum_est_min": float(np.min(caps)),
        "aum_est_max": float(np.max(caps)),
        "n_rebalance": len(caps),
        "median_adv": float(latest.median()) if not latest.empty else np.nan,
        "min_adv": float(latest.min()) if not latest.empty else np.nan,
        "assumption": "单票不超过 20 日均额的 10%，3 个交易日建仓；容量按成分股 10% 分位约束",
    }


def _filter_test(period_returns: pd.DataFrame, horizon: int) -> dict:
    """过滤价值检验：剔除 Q1 后组合是否真的变好。

    过滤的严格定义不是「Q1 最差」，而是「剔除 Q1 后夏普/回撤/尾部风险改善」。
    如果 Q1 的绝对收益与全组均值接近、剔除后夏普不升，则过滤只是放弃收益。
    """
    if period_returns is None or period_returns.empty or "Q1" not in period_returns:
        return {}
    cols = list(period_returns.columns)
    q1 = period_returns["Q1"].dropna()
    rest_cols = [c for c in cols if c != "Q1"]
    rest = period_returns[rest_cols].mean(axis=1).dropna()
    all_long = period_returns.mean(axis=1).dropna()
    if len(q1) < 20:
        return {}

    def _perf(series: pd.Series) -> dict:
        nav = (1 + series).cumprod()
        ann = float(nav.iloc[-1] ** (TRADING_DAYS / (len(series) * horizon)) - 1)
        vol = float(series.std() * np.sqrt(TRADING_DAYS / horizon))
        sharpe = float(series.mean() / series.std() * np.sqrt(TRADING_DAYS / horizon)) if series.std() else np.nan
        mdd = float((nav / nav.cummax() - 1).min())
        return {"ann": ann, "vol": vol, "sharpe": sharpe, "mdd": mdd}

    q1_perf, rest_perf, all_perf = _perf(q1), _perf(rest), _perf(all_long)
    spread = period_returns["Q5"] - period_returns["Q1"] if "Q5" in period_returns else pd.Series(dtype=float)
    spread = spread.dropna()
    se = float(spread.std(ddof=1) / np.sqrt(len(spread))) if len(spread) > 2 else np.nan
    rel = ((1 + q1).cumprod() / (1 + all_long).cumprod()).dropna()
    rel_mdd = float((rel / rel.cummax() - 1).min()) if len(rel) else np.nan
    tail_q1 = float((q1 < -0.03).mean())
    tail_rest = float((rest < -0.03).mean())
    ann_factor = TRADING_DAYS / horizon
    q1_excess = float(q1.mean() - all_long.mean())
    useful = bool(
        rest_perf["sharpe"] > all_perf["sharpe"] + 0.05
        and q1.mean() < all_long.mean() - 0.001
        and tail_q1 > tail_rest + 0.03
    )
    return {
        "q1_mean": float(q1.mean()),
        "rest_mean": float(rest.mean()),
        "all_mean": float(all_long.mean()),
        "q1_ann": float(q1.mean() * ann_factor),
        "rest_ann": float(rest.mean() * ann_factor),
        "all_ann": float(all_long.mean() * ann_factor),
        "q1_cagr": q1_perf["ann"],
        "rest_cagr": rest_perf["ann"],
        "all_cagr": all_perf["ann"],
        "q1_excess": q1_excess,
        "q1_excess_ann": float(q1_excess * ann_factor),
        "q1_sharpe": q1_perf["sharpe"],
        "rest_sharpe": rest_perf["sharpe"],
        "all_sharpe": all_perf["sharpe"],
        "q1_mdd": q1_perf["mdd"],
        "rel_mdd": rel_mdd,
        "tail_q1": tail_q1,
        "tail_rest": tail_rest,
        "spread_mean": float(spread.mean()) if len(spread) else np.nan,
        "spread_se": se,
        "useful": useful,
    }


def _annual_ic_return(ic_by_year: pd.Series | None, annual: pd.Series | None) -> pd.DataFrame | None:
    if ic_by_year is None or annual is None:
        return None
    frame = pd.concat([ic_by_year.rename("ic"), annual.rename("ret")], axis=1).dropna()
    if frame.empty:
        return None
    return frame


def _corr_p(r: float, n: int) -> float:
    """Fisher z 近似的相关系数双尾 p 值（不依赖 scipy）。"""
    if not np.isfinite(r) or n < 4:
        return float("nan")
    from math import atanh, erfc, sqrt

    z = atanh(max(-0.999999, min(0.999999, r))) * sqrt(n - 3)
    return float(erfc(abs(z) / sqrt(2)))


def _incremental(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    peers: dict[str, pd.DataFrame] | None,
) -> tuple[dict, pd.Series | None]:
    """增量性：与同类因子的相关性 + 剔除同类后的残差 IC。"""
    if not peers:
        return {}, None
    dates = factor.index
    sample = dates[:: max(1, len(dates) // 120)]
    corrs = {}
    for name, panel in peers.items():
        if panel is None or panel.empty:
            continue
        values = []
        for dt in sample:
            if dt not in panel.index or dt not in factor.index:
                continue
            a = factor.loc[dt]
            b = panel.loc[dt]
            pair = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
            if len(pair) >= 30 and pair["a"].nunique() > 5 and pair["b"].nunique() > 5:
                values.append(pair["a"].rank().corr(pair["b"].rank()))
        if values:
            corrs[name] = float(np.nanmean(values))
    if not corrs:
        return {}, None
    corr_series = pd.Series(corrs).sort_values(key=lambda s: s.abs(), ascending=False)

    # 残差 IC：每个截面把因子对全部同类因子回归，取残差
    peer_names = list(corr_series.index)
    residual = factor.copy()
    values = residual.to_numpy(dtype="float64")
    peer_values = [peers[n].reindex_like(factor).to_numpy(dtype="float64") for n in peer_names]
    for i in range(len(dates)):
        y = values[i]
        cols = [p[i] for p in peer_values]
        valid = np.isfinite(y)
        for c in cols:
            valid &= np.isfinite(c)
        if valid.sum() < 40:
            continue
        x = np.column_stack([np.ones(valid.sum())] + [c[valid] for c in cols])
        try:
            beta, *_ = np.linalg.lstsq(x, y[valid], rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = y[valid] - x @ beta
        values[i, valid] = resid
    residual = pd.DataFrame(values, index=factor.index, columns=factor.columns)
    ic_raw = float(_rank_ic_frame(factor, forward).mean())
    ic_resid = float(_rank_ic_frame(residual, forward).mean())
    raw_ratio = ic_resid / ic_raw if np.isfinite(ic_raw) and ic_raw != 0 else np.nan
    max_corr = float(corr_series.abs().max())
    # 只有「原始 IC 为正 且 残差 IC 为正」时，保留率才有经济含义。
    # 负/负 会得到 +270% 这种假高分；原始方向无效时增量性直接 0。
    effective = raw_ratio if (np.isfinite(raw_ratio) and ic_raw > 0 and ic_resid > 0) else 0.0
    base = float(np.clip(effective, 0, 1) * 10)
    diversity = float(np.clip((0.90 - max_corr) / 0.40, 0, 1) * 5)
    # 残差为负或与同类几乎相同：不是「保留率低」，而是重复因子，增量性应为 0
    if (np.isfinite(raw_ratio) and raw_ratio < 0) or max_corr >= 0.95 or ic_raw <= 0:
        score = 0.0
    else:
        score = float(np.clip(base + diversity, 0, 15))
    return {
        "peers": corr_series,
        "max_corr": max_corr,
        "ic_raw": ic_raw,
        "ic_resid": ic_resid,
        "ratio": raw_ratio,
        "effective_ratio": effective,
        "score": score,
        "residual": residual,
    }, corr_series


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
    regime_alt: pd.Series | None = None,
    market_cap: pd.DataFrame | None = None,
    industry: pd.DataFrame | None = None,
    amount: pd.DataFrame | None = None,
    benchmark: pd.Series | None = None,
    peers: dict[str, pd.DataFrame] | None = None,
    definition: dict | None = None,
    pool: dict | None = None,
    freshness: dict | None = None,
    is_st: dict[str, bool] | None = None,
    exclude_limit_up: bool = False,
    direction_source: str = "registry",
    full: bool = True,
) -> FactorReport:
    factor = factor.sort_index()
    # 因子面板可能比行情短（预热期被裁掉）。统一裁到因子日期，避免
    # 可成交分母、成本、容量把预热期算进去，也避免 fwd 跨到评估起点之前。
    if not factor.index.equals(close.index):
        close = close.reindex(factor.index)
        open_ = open_.reindex(factor.index)
    # 有效起点：横截面至少 20%（且不少于 30 只）有因子值。财务 value 因子
    # 2023 才有数据，但面板从 2019 开始；不裁会让 report.start/n_dates/覆盖率
    # 都错报成 2019 起、44% 覆盖，并误触发覆盖率否决。
    counts = factor.notna().sum(axis=1)
    min_names = max(30, int(0.2 * factor.shape[1]))
    usable = counts[counts >= min_names]
    if len(usable):
        effective_start = usable.index.min()
        factor = factor.loc[factor.index >= effective_start]
        close = close.loc[close.index >= effective_start]
        open_ = open_.loc[open_.index >= effective_start]
    fwd = forward_returns(close, open_, horizons)
    mask = tradeable_mask(close, open_, is_st=is_st)

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
        definition=definition or {},
        pool=pool or {},
        freshness=freshness or {},
        raw_direction=direction,
    )

    # ---- 方向：唯一入口。
    # 因子值本身不取负；分析信号 = raw × direction。**不做样本内翻转**：
    # 在同一样本上按 IC 符号选方向，等价于用未来信息择向，会把随机因子的 IC
    # 系统性抬成正数（旧版 4/4 强因子 IC>0、ρ 与价差打架，根因就在这里）。
    if direction not in (-1, 1):
        raise ValueError(f"direction 必须是 +1/-1，收到 {direction!r}")
    analysis_factor = factor * int(direction)
    report.direction = int(direction)
    report.raw_direction = int(direction)
    report.direction_source = direction_source
    report.flipped = False

    valid_values = factor.to_numpy(dtype="float64")
    finite = valid_values[np.isfinite(valid_values)]
    if len(finite):
        report.factor_stats = {
            "median": float(np.median(finite)),
            "p01": float(np.quantile(finite, 0.01)),
            "p99": float(np.quantile(finite, 0.99)),
            "skew": float(pd.Series(finite).skew()),
            "min": float(finite.min()),
            "max": float(finite.max()),
            "missing": float(1 - len(finite) / valid_values.size),
            "n": int(len(finite)),
        }
    # 可成交过滤：t+1 开盘一字涨停/停牌的样本不可买入，IC 与分层都必须剔除
    fwd_raw = dict(fwd)
    if exclude_limit_up:
        fwd = {h: v.where(mask) for h, v in fwd.items()}
    report.limit_up = {
        "applied": bool(exclude_limit_up),
        "excluded_ratio": float((~mask).to_numpy().mean()),
        "ic_raw": float(_rank_ic_frame(analysis_factor, fwd_raw[primary_horizon]).mean()),
        "ic_masked": float(_rank_ic_frame(analysis_factor, fwd[primary_horizon]).mean()),
    }

    # 横截面收益离散度：IC 传导到 Q5−Q1 的分母（报告里不再写死 7%）
    sigma_series = fwd[primary_horizon].std(axis=1, skipna=True)
    sigma_series = sigma_series[fwd[primary_horizon].notna().sum(axis=1) >= 30]
    report.sigma_5d = float(sigma_series.mean()) if len(sigma_series) else float("nan")

    rows = []
    for h in horizons:
        series = _rank_ic_frame(analysis_factor, fwd[h])
        report.ic[h] = series
        if series.empty:
            continue
        icir = series.mean() / (series.std() or np.nan) * np.sqrt(TRADING_DAYS / h)
        t_indep, n_indep = independent_t(series, step=h)
        lo, hi = block_bootstrap_ci(series, block=max(5, h))
        rows.append(
            {
                "h": h,
                "mean": series.mean(),
                "std": series.std(),
                "ir": icir,
                "t": newey_west_t(series, lag=max(1, h - 1)),
                "t_indep": t_indep,
                "n_indep": n_indep,
                "ci_lo": lo,
                "ci_hi": hi,
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
        half = len(primary) // 2
        if half >= 20:
            first, second = primary.iloc[:half], primary.iloc[half:]
            report.ic_half = {
                "first": float(first.mean()),
                "second": float(second.mean()),
                "first_n": int(len(first)),
                "second_n": int(len(second)),
                "same_sign": bool(np.sign(first.mean()) == np.sign(second.mean())),
                "first_end": str(first.index.max().date()),
                "second_start": str(second.index.min().date()),
            }
        if regime is not None:
            aligned = regime.reindex(primary.index).dropna()
            if not aligned.empty:
                report.ic_by_regime = primary.reindex(aligned.index).groupby(aligned).mean()
                report.regime_stats = _regime_table(primary, regime)
                if regime_alt is not None:
                    alt = regime_alt.reindex(primary.index).dropna()
                    if not alt.empty:
                        report.regime_stats_alt = _regime_table(primary, regime_alt)
                tmp = pd.DataFrame({"ic": primary, "regime": regime.reindex(primary.index)}).dropna()
                if not tmp.empty:
                    report.regime_share = (
                        tmp.groupby([tmp.index.year, "regime"]).size().unstack(fill_value=0)
                    )
                    report.regime_share = report.regime_share.div(
                        report.regime_share.sum(axis=1).replace(0, np.nan), axis=0
                    )
                    if "bull" in report.regime_share.columns and report.ic_by_year is not None:
                        merged = pd.concat(
                            [report.ic_by_year.rename("ic"), report.regime_share["bull"].rename("bull")],
                            axis=1,
                        ).dropna()
                        if len(merged) >= 4 and merged["bull"].nunique() > 1:
                            report.regime_corr = float(merged["ic"].corr(merged["bull"]))
                            report.regime_corr_p = _corr_p(report.regime_corr, len(merged))
        report.decay = pd.Series({h: s.mean() for h, s in report.ic.items()})
        report.decay_note = _decay_note(report.decay)

        year_rows = {}
        for year, group in primary.groupby(primary.index.year):
            lo, hi = block_bootstrap_ci(group, block=5)
            year_rows[int(year)] = {
                "mean": float(group.mean()),
                "n": int(len(group)),
                "ci_lo": lo,
                "ci_hi": hi,
            }
        report.ic_by_year_ci = pd.DataFrame(year_rows).T

    nav, turnover_df, period_returns, period_gross, daily, ls_cost, turnover_mean = _quantile_nav(
        analysis_factor, close, open_, groups=groups, horizon=primary_horizon, cost_bps=cost_bps,
        tradeable=mask if exclude_limit_up else None,
    )
    report.nav = nav
    report.daily_returns = daily
    report.period_returns = period_returns
    report.period_returns_gross = period_gross
    report.turnover_by_group = turnover_mean
    report.period_returns_net = period_returns
    if not period_returns.empty and not period_gross.empty:
        # 分层优劣用「每期平均收益」，不用复利终值（避免波动拖累污染单调性）。
        # 单调性用**毛收益**：净收益被各组换手差异污染，会把方向判断带偏。
        means_net = period_returns.mean()
        means_gross = period_gross.mean()
        report.quantile_returns = {str(k): float(v) for k, v in means_net.items()}
        report.quantile_returns_gross = {str(k): float(v) for k, v in means_gross.items()}
        report.quantile_ci = {}
        for col in period_returns.columns:
            series = period_returns[col].dropna()
            if len(series) >= 10:
                se = series.std(ddof=1) / np.sqrt(len(series))
                report.quantile_ci[col] = (float(series.mean() - 1.96 * se), float(series.mean() + 1.96 * se))
        if len(means_gross) >= 3:
            report.monotonicity, report.monotonicity_p = diag.group_monotonicity(means_gross)
            report.monotonicity_net, _ = diag.group_monotonicity(means_net)

        q_high, q_low = f"Q{groups}", "Q1"
        gross_spread = (period_gross[q_high] - period_gross[q_low]).dropna()
        cost_series = (
            (turnover_df.get(q_high, pd.Series(dtype="float"))
             + turnover_df.get(q_low, pd.Series(dtype="float"))) * cost_bps / 10000.0
        ).dropna()
        # 多空净价差 = 毛价差 − 两条腿的换手成本。不能用 Q5_net − Q1_net：
        # 两条腿成本相同会互相抵消，等于漏算空头腿成本。
        net_spread = (gross_spread - cost_series).dropna()
        report.spread_gross = diag.spread_stats(gross_spread)
        report.spread_net = diag.spread_stats(net_spread)
        report.cost_mean = float(cost_series.mean()) if len(cost_series) else float("nan")
        report.filter_test = _filter_test(period_returns, primary_horizon)
        if len(net_spread) > 10:
            ls_nav = (1 + net_spread).cumprod()
            report.ls_nav = ls_nav
            report.ls_daily = net_spread
            report.drawdown = ls_nav / ls_nav.cummax() - 1
            report.perf = _period_performance(net_spread, primary_horizon)
            report.annual_returns = _annual_returns(ls_nav)
            report.annual_ic_return = _annual_ic_return(report.ic_by_year, report.annual_returns)
            report.long_short = {
                "total": report.perf.get("total", np.nan),
                "ann": report.perf.get("ann", np.nan),
                "sharpe": report.perf.get("sharpe", np.nan),
                "mdd": report.perf.get("mdd", np.nan),
                "calmar": report.perf.get("calmar", np.nan),
                "win": report.perf.get("win", np.nan),
                "geo_period": report.perf.get("ann", np.nan),
                "ls_spread_mean": float(net_spread.mean()),
                "gross_spread_mean": report.spread_gross["mean"],
                "cost_mean": report.cost_mean,
                "net_spread_mean": report.spread_net["mean"],
                "spread_t": report.spread_net["t"],
                "spread_gross_t": report.spread_gross["t"],
            }
            if report.filter_test:
                report.filter_test["spread_mean"] = float(net_spread.mean())
                report.filter_test["spread_se"] = report.spread_net["se"]
                report.filter_test["spread_gross_mean"] = report.spread_gross["mean"]
                report.filter_test["spread_gross_se"] = report.spread_gross["se"]
    report.turnover = (
        float(turnover_mean.get(f"Q{groups}", turnover_mean.mean()))
        if not turnover_mean.empty else float("nan")
    )
    if benchmark is not None and not benchmark.empty:
        bench = benchmark.reindex(close.index).ffill().dropna()
        if not bench.empty:
            report.benchmark_nav = bench / bench.iloc[0]

    if full and (market_cap is not None or industry is not None):
        report.ic_neutral = neutralized_ic(
            analysis_factor, fwd[primary_horizon], market_cap, industry
        )
        if industry is not None:
            ind = report.ic_neutral.loc["行业中性", "mean"] if "行业中性" in report.ic_neutral.index else np.nan
            report.industry_neutral_ic = float(ind)

    if market_cap is not None:
        ic_by_size = {}
        caps = market_cap.reindex(index=factor.index, columns=factor.columns)
        for label, lo, hi in (("小市值", 0.0, 1 / 3), ("中市值", 1 / 3, 2 / 3), ("大市值", 2 / 3, 1.0)):
            rank = caps.rank(axis=1, pct=True)
            masked = analysis_factor.where((rank >= lo) & (rank <= hi))
            series = _rank_ic_frame(masked, fwd[primary_horizon])
            if len(series) > 20:
                ic_by_size[label] = float(series.mean())
        report.ic_by_size = pd.Series(ic_by_size) if ic_by_size else None

    if industry is not None:
        industry = industry.reindex(index=factor.index, columns=factor.columns)
        ic_by_ind = {}
        for name in pd.unique(industry.iloc[-1].dropna()):
            if str(name) in ("未知", "未分类"):
                continue
            mask_ind = industry == name
            masked = analysis_factor.where(mask_ind)
            series = _rank_ic_frame(masked, fwd[primary_horizon], min_names=8)
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

    report.tradeability = _tradeability(factor, close, open_, horizon=primary_horizon, is_st=is_st)
    if full:
        report.sell_side = _sell_side_summary(
            analysis_factor, close, open_, fwd[primary_horizon], horizon=primary_horizon, groups=groups, is_st=is_st
        )
        report.cost_table = _cost_sensitivity(
            analysis_factor, close, open_, groups=groups, horizon=primary_horizon,
            tradeable=mask if exclude_limit_up else None,
        )
        report.capacity = _capacity(amount, analysis_factor, primary_horizon, groups) if amount is not None else {}
        inc, corr = _incremental(analysis_factor, fwd[primary_horizon], peers)
        report.incrementality = inc
        report.peers = corr

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
    if report.perf.get("mdd", 0) < -0.40:
        report.warnings.append(
            f"多空最大回撤 {report.perf['mdd']:.1%}，波动极大；需按回撤预算决定是否可用"
        )

    # ---- 结构化自检：结论必须能被程序复核，而不是只写在正文里 ----
    ic5 = float(report.ic_stats.loc[primary_horizon, "mean"]) if primary_horizon in report.ic_stats.index else float("nan")
    t5 = float(report.ic_stats.loc[primary_horizon, "t_indep"]) if primary_horizon in report.ic_stats.index else float("nan")
    if not np.isfinite(t5):
        t5 = float(report.ic_stats.loc[primary_horizon, "t"]) if primary_horizon in report.ic_stats.index else float("nan")
    gross = report.spread_gross or {}
    report.transmission = diag.ic_spread_transmission(ic5, report.sigma_5d, gross.get("mean", np.nan)).as_dict()
    checks = [
        diag.rho_spread_identity(report.monotonicity, gross.get("mean", np.nan), groups),
        diag.ic_spread_transmission(ic5, report.sigma_5d, gross.get("mean", np.nan)),
        diag.tradeability_interval(gross, report.cost_mean),
        diag.direction_prior(ic5, t5, report.direction),
    ]
    report.diagnostics = checks
    report.attribution = diag.attribute_failure(report)
    for check in checks:
        if check.status == "fail":
            report.warnings.append(f"自检失败[{check.check_id}]：{check.actual}")
        elif check.status == "warn":
            report.warnings.append(f"自检提示[{check.check_id}]：{check.actual}")
    return report


def _tradeability(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    horizon: int,
    is_st: dict[str, bool] | None,
) -> dict:
    """买入端可成交性：分子/分母逐项列清，并给出与旧口径的对账。"""
    dates = factor.index
    n = len(dates)
    limit_up, _ = _limit_matrix(close, is_st)
    nxt_open = open_.shift(-1)
    valid = factor.notna()
    has_next = pd.DataFrame(
        np.repeat((np.arange(n) < n - 1)[:, None], factor.shape[1], axis=1),
        index=dates, columns=factor.columns,
    )
    denom_mask = valid & has_next
    suspended = denom_mask & nxt_open.isna()
    limit_blocked = denom_mask & nxt_open.notna() & (nxt_open >= limit_up - 0.005)
    blocked = suspended | limit_blocked

    old_mask = tradeable_mask(close, open_, is_st=is_st)
    old_blocked = ~old_mask
    old_denom = int(old_mask.size)
    old_num = int(old_blocked.to_numpy().sum())
    new_denom = int(denom_mask.to_numpy().sum())
    new_num = int(blocked.to_numpy().sum())

    return {
        "denominator": new_denom,
        "suspended": int(suspended.to_numpy().sum()),
        "limit_up": int(limit_blocked.to_numpy().sum()),
        "blocked": new_num,
        "pct": new_num / new_denom if new_denom else np.nan,
        "old_denominator": old_denom,
        "old_blocked": old_num,
        "old_pct": old_num / old_denom if old_denom else np.nan,
        "excluded_cells": old_denom - new_denom,
        "excluded_blocked": old_num - new_num,
        "explain": (
            "旧口径分母=全部面板单元（含上市前空值、因子预热期）；"
            "新口径分母=有因子值且存在下一交易日的信号。"
        ),
    }


def _sell_side_summary(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    planned: pd.DataFrame,
    *,
    horizon: int,
    groups: int,
    is_st: dict[str, bool] | None,
) -> dict:
    delayed, delay, blocked0 = _delayed_exit(close, open_, horizon, is_st=is_st)
    mature = pd.DataFrame(
        np.repeat((np.arange(len(factor.index)) + horizon < len(factor.index))[:, None],
                  factor.shape[1], axis=1),
        index=factor.index, columns=factor.columns,
    )
    entry_valid = open_.shift(-1).notna() & factor.notna() & mature
    blocked_target = entry_valid & blocked0
    denom = int(entry_valid.to_numpy().sum())
    delayed_cnt = int((delay > 0).to_numpy().sum())
    stats = {
        "denominator": denom,
        "blocked_at_target": int(blocked_target.to_numpy().sum()),
        "pct_blocked": float(blocked_target.to_numpy().sum() / denom) if denom else np.nan,
        "delayed": delayed_cnt,
        "pct_delayed": float(delayed_cnt / denom) if denom else np.nan,
        "avg_delay": float(delay.where(delay > 0).stack().mean()) if delayed_cnt else 0.0,
        "max_delay": float(delay.max().max()) if delayed_cnt else 0.0,
    }
    # 组合层面：计划卖出 vs 顺延卖出的 Q5−Q1 每期均值
    def _spread(ret: pd.DataFrame) -> float:
        vals = []
        dates = factor.index
        i = 0
        while i + horizon + 1 < len(dates):
            dt = dates[i]
            row = factor.loc[dt].dropna()
            if len(row) >= groups * 5:
                ranked = row.rank(method="first")
                buckets = pd.qcut(ranked, groups, labels=False, duplicates="drop")
                top = list(row.index[buckets == groups - 1])
                bottom = list(row.index[buckets == 0])
                r = ret.loc[dt]
                t = r.reindex(top).mean()
                b = r.reindex(bottom).mean()
                if np.isfinite(t) and np.isfinite(b):
                    vals.append(float(t - b))
            i += horizon
        return float(np.mean(vals)) if vals else np.nan

    planned_spread = _spread(planned)
    delayed_spread = _spread(delayed)
    stats["spread_planned"] = planned_spread
    stats["spread_delayed"] = delayed_spread
    stats["spread_delta"] = delayed_spread - planned_spread
    return stats


def _cost_sensitivity(
    factor: pd.DataFrame,
    close: pd.DataFrame,
    open_: pd.DataFrame,
    *,
    groups: int,
    horizon: int,
    tradeable: pd.DataFrame | None,
) -> pd.DataFrame:
    rows = {}
    for bps in (0.0, 10.0, 20.0, 30.0, 50.0):
        _, turnover_df, _, periods_gross, _, _, _ = _quantile_nav(
            factor, close, open_, groups=groups, horizon=horizon, cost_bps=bps, tradeable=tradeable
        )
        spread = (
            periods_gross.get(f"Q{groups}", pd.Series(dtype="float"))
            - periods_gross.get("Q1", pd.Series(dtype="float"))
            - (turnover_df.get(f"Q{groups}", pd.Series(dtype="float"))
               + turnover_df.get("Q1", pd.Series(dtype="float"))) * bps / 10000.0
        ).dropna()
        perf = _period_performance(spread, horizon)
        rows[f"{bps:.0f}bp"] = {
            "ann": perf.get("ann", np.nan),
            "sharpe": perf.get("sharpe", np.nan),
            "mdd": perf.get("mdd", np.nan),
        }
    return pd.DataFrame(rows).T
