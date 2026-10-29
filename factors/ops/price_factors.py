"""价量因子的**计算层**（纯 pandas，不碰 DB / 元数据）。

分层约定
--------
- 本文件只负责「给定单标的日线 frame，返回一条 Series」；
- **分类与元数据（Category / factor_id / 窗口 / 方向 / 公式）在**
  ``factor_engine/definitions/price_factors.py``，那里才登记进 registry；
- 这样 ``factors/`` 保持纯计算，``factor_engine/`` 负责编排，符合仓库既有分层。

覆盖：Liquidity 35、Momentum 20、Reversal 3、Risk 25、Size 3（共 86）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..library import factor
from ..naming import house_id

_EPS = 1e-12

# ---------------------------------------------------------------- 基础工具
def _col(frame: pd.DataFrame, name: str, default=np.nan) -> pd.Series:
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(default, index=frame.index, dtype="float64")

def _close(frame: pd.DataFrame) -> pd.Series:
    return _col(frame, "hfq_close") if "hfq_close" in frame.columns else _col(frame, "close")

def _ret(frame: pd.DataFrame) -> pd.Series:
    return _close(frame).pct_change(fill_method=None)

def _turnover(frame: pd.DataFrame) -> pd.Series:
    if "turnover" in frame.columns:
        series = pd.to_numeric(frame["turnover"], errors="coerce")
        if series.notna().any():
            return series
    return (_col(frame, "volume") / _col(frame, "float_share").replace(0, np.nan)).replace(
        [np.inf, -np.inf], np.nan
    )

def _volcap(frame: pd.DataFrame) -> pd.Series:
    return (_col(frame, "volume") / _col(frame, "float_mv").replace(0, np.nan)).replace(
        [np.inf, -np.inf], np.nan
    )

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()

def _benchmark(frame: pd.DataFrame) -> pd.Series:
    for name in ("benchmark_close", "bm_close", "index_close"):
        if name in frame.columns:
            series = pd.to_numeric(frame[name], errors="coerce")
            if series.notna().any():
                return series
    raise ValueError(
        "该因子需要基准指数序列：请在因子帧中提供 benchmark_close 列"
        "（build_panel 前合并指数收盘价即可）"
    )

def _benchmark_volume(frame: pd.DataFrame) -> pd.Series:
    for name in ("benchmark_volume", "bm_volume"):
        if name in frame.columns:
            series = pd.to_numeric(frame[name], errors="coerce")
            if series.notna().any():
                return series
    raise ValueError("该因子需要 benchmark_volume 列")

def _rolling_beta(y: pd.Series, x: pd.Series, window: int) -> pd.Series:
    n = max(10, window // 3)
    cov = y.rolling(window, min_periods=n).cov(x)
    var = x.rolling(window, min_periods=n).var()
    return (cov / var.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

def _rolling_alpha(y: pd.Series, x: pd.Series, window: int) -> pd.Series:
    n = max(10, window // 3)
    beta = _rolling_beta(y, x, window)
    return (
        y.rolling(window, min_periods=n).mean()
        - beta * x.rolling(window, min_periods=n).mean()
    )

def _streak(flags: pd.Series) -> pd.Series:
    values = flags.fillna(False).to_numpy(dtype=bool)
    out = np.zeros(len(values), dtype="float64")
    count = 0
    for i, flag in enumerate(values):
        count = count + 1 if flag else 0
        out[i] = count
    return pd.Series(out, index=flags.index)

def _bias(num: pd.Series, den: pd.Series) -> pd.Series:
    return (num / den.replace(0, np.nan) - 1).replace([np.inf, -np.inf], np.nan)

# ---------------------------------------------------------------- Liquidity
def _f_avg_turnover(n: int):
    return lambda f: _turnover(f).rolling(n, min_periods=max(2, n // 3)).mean()

def _f_std_turnover(n: int):
    return lambda f: _turnover(f).rolling(n, min_periods=max(5, n // 3)).std()

def _f_bias_turn(short: int, long_: int):
    def fn(f):
        t = _turnover(f)
        return _bias(
            t.rolling(short, min_periods=max(2, short // 3)).mean(),
            t.rolling(long_, min_periods=max(20, long_ // 3)).mean(),
        )
    return fn

def _f_bias_std_turn(short: int, long_: int):
    def fn(f):
        t = _turnover(f)
        return _bias(
            t.rolling(short, min_periods=max(5, short // 3)).std(),
            t.rolling(long_, min_periods=max(20, long_ // 3)).std(),
        )
    return fn

def _f_sum_abs_rtn_amount(n: int = 20):
    def fn(f):
        num = _ret(f).abs().rolling(n, min_periods=5).sum()
        den = _col(f, "amount").rolling(n, min_periods=5).sum()
        return (num / den.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return fn

def _f_volume_alpha(window: int):
    def fn(f):
        vol = _col(f, "volume")
        bench_vol = _benchmark_volume(f)
        stock_g = vol.rolling(5).sum().pct_change(fill_method=None)
        bench_g = bench_vol.rolling(5).sum().pct_change(fill_method=None)
        return _rolling_alpha(stock_g, bench_g, window)
    return fn

# ---------------------------------------------------------------- Momentum
def _f_return(n: int):
    return lambda f: _close(f) / _close(f).shift(n) - 1

def _price_position_ir(window: int = 60):
    def fn(f):
        ratio = (_close(f) - _col(f, "open")) / (
            _col(f, "high") - _col(f, "low")
        ).replace(0, np.nan)
        mean = ratio.rolling(window, min_periods=20).mean()
        std = ratio.rolling(window, min_periods=20).std()
        return (mean / std.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return fn

def _f_alpha(window: int):
    def fn(f):
        bench = _benchmark(f)
        return _rolling_alpha(_ret(f), bench.pct_change(fill_method=None), window)
    return fn

def _rsrs(f):
    high = _col(f, "hfq_high") if "hfq_high" in f.columns else _col(f, "high")
    low = _col(f, "hfq_low") if "hfq_low" in f.columns else _col(f, "low")
    slope = _rolling_beta(low, high, 18)
    mean = slope.rolling(200, min_periods=60).mean()
    std = slope.rolling(200, min_periods=60).std()
    return (slope - mean) / std.replace(0, np.nan)

def _macd_parts(f):
    close = _close(f)
    dif = _ema(close, 12) - _ema(close, 26)
    return dif, _ema(dif, 9)

def _days_down_up(f):
    ret = _ret(f)
    return (_streak(ret > 0) - _streak(ret < 0) - 1).abs()

# ---------------------------------------------------------------- Reversal
def _rsi(f, period: int = 14):
    delta = _close(f).diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder((-delta).clip(lower=0), period)
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

def _price_dist(f):
    close = _close(f)
    values = close.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    for i, price in enumerate(values):
        if not np.isfinite(price) or price <= 0:
            continue
        if price < 10:
            out[i] = np.ceil(price) - price
        elif price < 100:
            out[i] = (np.ceil(price / 10.0) * 10.0 - price) / price
        else:
            out[i] = (np.ceil(price / 100.0) * 100.0 - price) / price
    return pd.Series(out, index=close.index)

# ---------------------------------------------------------------- Risk
def _f_return_std(n: int):
    return lambda f: _ret(f).rolling(n, min_periods=max(10, n // 4)).std()

def _f_high_low(n: int):
    def fn(f):
        high = _col(f, "hfq_high") if "hfq_high" in f.columns else _col(f, "high")
        low = _col(f, "hfq_low") if "hfq_low" in f.columns else _col(f, "low")
        hi = high.rolling(n, min_periods=max(5, n // 4)).max()
        lo = low.rolling(n, min_periods=max(5, n // 4)).min()
        return (hi / lo.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return fn

def _f_sharpe(window: int):
    def fn(f):
        r = _ret(f)
        mean = r.rolling(window, min_periods=max(20, window // 4)).mean()
        std = r.rolling(window, min_periods=max(20, window // 4)).std()
        return (mean / std.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    return fn

def _f_beta(window: int):
    def fn(f):
        bench = _benchmark(f)
        return _rolling_beta(_ret(f), bench.pct_change(fill_method=None), window)
    return fn

def _days_beyond(f, window: int = 21):
    close = _close(f)
    mean = close.rolling(window, min_periods=5).mean()
    std = close.rolling(window, min_periods=5).std()
    z = (close - mean) / std.replace(0, np.nan)
    upper = (z > 1).astype("float64").rolling(window, min_periods=5).sum()
    lower = (z < -1).astype("float64").rolling(window, min_periods=5).sum()
    return upper - lower

def _adjusted_sharpe(f, window: int = 750):
    r = _ret(f)
    mean = r.rolling(window, min_periods=100).mean()
    std = r.rolling(window, min_periods=100).std()
    return (mean / (std ** 4).replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

def _volume_beta(f, window: int = 120, sum_window: int = 5):
    vol = _col(f, "volume")
    bench_vol = _benchmark_volume(f)
    stock_g = vol.rolling(sum_window).sum().pct_change(fill_method=None)
    bench_g = bench_vol.rolling(sum_window).sum().pct_change(fill_method=None)
    return _rolling_beta(stock_g, bench_g, window)

def _sigma(f, window: int = 1320):
    bench = _benchmark(f)
    y, x = _ret(f), bench.pct_change(fill_method=None)
    beta = _rolling_beta(y, x, window)
    return (y - beta * x).rolling(window, min_periods=60).std()

def _beta_consistency(f, window: int = 1320):
    bench = _benchmark(f)
    y, x = _ret(f), bench.pct_change(fill_method=None)
    beta = _rolling_beta(y, x, window)
    return (beta * (y - beta * x)).rolling(window, min_periods=60).std()

# ---------------------------------------------------------------- 注册表
#: 按类别分组的 (计算函数, 窗口, 单位)
LIQUIDITY: dict[str, tuple] = {}
MOMENTUM: dict[str, tuple] = {}
REVERSAL: dict[str, tuple] = {}
RISK: dict[str, tuple] = {}
SIZE: dict[str, tuple] = {}

for _n in (5, 10, 20, 21, 42, 63, 126, 252):
    LIQUIDITY[f"avg_turnover_{_n}d"] = (_f_avg_turnover(_n), _n, "pct")
for _n in (21, 42, 63, 126, 252):
    LIQUIDITY[f"std_turnover_{_n}d"] = (_f_std_turnover(_n), _n, "pct")
for _s, _l in ((21, 252), (21, 504), (42, 252), (42, 504), (63, 252), (63, 504),
               (126, 252), (126, 504)):
    LIQUIDITY[f"bias_turn_{_s}d_{_l}d"] = (_f_bias_turn(_s, _l), _l, "ratio")
    LIQUIDITY[f"bias_std_turn_{_s}d_{_l}d"] = (_f_bias_std_turn(_s, _l), _l, "ratio")

LIQUIDITY["amount_ma_20d"] = (
    lambda f: _col(f, "amount").rolling(20, min_periods=5).mean(), 20, "currency")
LIQUIDITY["turnover_ma_20d"] = (
    lambda f: -_volcap(f).rolling(20, min_periods=5).mean(), 20, "ratio")
LIQUIDITY["turnover_ma_20d_120d"] = (
    lambda f: -_volcap(f).rolling(20, min_periods=5).mean()
    / _volcap(f).rolling(120, min_periods=20).mean().replace(0, np.nan), 120, "ratio")
LIQUIDITY["sum_abs_rtn_amount_20d"] = (_f_sum_abs_rtn_amount(20), 20, "ratio")
LIQUIDITY["volume_alpha_300d_000001"] = (_f_volume_alpha(300), 300, "ratio")
LIQUIDITY["volume_alpha_300d_000300"] = (_f_volume_alpha(300), 300, "ratio")

for _n in (5, 21, 42, 63, 126, 252):
    MOMENTUM[f"return_{_n}d"] = (_f_return(_n), _n, "ratio")
MOMENTUM["ma_20d"] = (lambda f: _close(f).rolling(20, min_periods=5).mean(), 20, "price")
MOMENTUM["price_position_ir_60d"] = (_price_position_ir(60), 60, "ratio")
for _w, _name in ((125, "alpha_125d_000300"), (250, "alpha_250d_000300"),
                  (500, "alpha_500d_000300"), (1000, "alpha_1000d_000300"),
                  (528, "alpha_528d_000001"), (792, "alpha_792d_000001"),
                  (1320, "alpha_1320d_000001")):
    MOMENTUM[_name] = (_f_alpha(_w), _w, "ratio")
MOMENTUM["rsrs"] = (_rsrs, 218, "ratio")
MOMENTUM["MACD"] = (lambda f: 2 * (_macd_parts(f)[0] - _macd_parts(f)[1]), 35, "price")
MOMENTUM["dif"] = (lambda f: _macd_parts(f)[0], 26, "price")
MOMENTUM["dea"] = (lambda f: _macd_parts(f)[1], 35, "price")
MOMENTUM["days_down_up"] = (_days_down_up, 20, "days")

REVERSAL["small_cap_reversal_21d"] = (
    lambda f: -(_close(f) / _close(f).shift(21) - 1), 21, "ratio")
REVERSAL["rsi"] = (_rsi, 14, "pct")
REVERSAL["price_dist"] = (_price_dist, 1, "ratio")

RISK["days_beyond_upper_lower_21d"] = (_days_beyond, 21, "days")
for _n in (21, 42, 63, 126, 252):
    RISK[f"return_std_{_n}d"] = (_f_return_std(_n), _n, "ratio")
    RISK[f"high_low_{_n}d"] = (_f_high_low(_n), _n, "ratio")
RISK["sharpe_60d"] = (_f_sharpe(60), 60, "ratio")
RISK["sharpe_750d"] = (_f_sharpe(750), 750, "ratio")
RISK["adjusted_sharpe_750d"] = (_adjusted_sharpe, 750, "ratio")
for _w, _name in ((60, "beta_60d_000300"), (125, "beta_125d_000300"),
                  (250, "beta_250d_000300"), (500, "beta_500d_000300"),
                  (1000, "beta_1000d_000300"), (1320, "beta_1320d_000001")):
    RISK[_name] = (_f_beta(_w), _w, "ratio")
RISK["volume_beta_120d_000300"] = (_volume_beta, 120, "ratio")
RISK["sigma_1320d_000001"] = (_sigma, 1320, "ratio")
RISK["sigma_1320d_000300"] = (_sigma, 1320, "ratio")
RISK["beta_consistency_1320d_000300"] = (_beta_consistency, 1320, "ratio")
RISK["log_price"] = (lambda f: np.log(_close(f).clip(lower=_EPS)), 1, "ratio")

SIZE["size"] = (lambda f: -np.log(_col(f, "total_mv").clip(lower=_EPS) / 1e6), 1, "ratio")
SIZE["float_size"] = (lambda f: -np.log(_col(f, "float_mv").clip(lower=_EPS) / 1e6), 1, "ratio")

#: 类别 -> 因子表（factor_engine 据此生成 FactorSpec）
GROUPS: dict[str, dict[str, tuple]] = {
    "liquidity": LIQUIDITY,
    "momentum": MOMENTUM,
    "reversal": REVERSAL,
    "risk": RISK,
    "size": SIZE,
}

#: 逐标的无法计算、需要横截面回归的因子
CROSS_SECTION_ONLY = ("nl_size",)

ALL_NAMES: tuple[str, ...] = tuple(
    sorted(house_id(category, name) for category, group in GROUPS.items() for name in group)
) + tuple(house_id("size", name) for name in CROSS_SECTION_ONLY)

def register_price_ops() -> list[str]:
    """按**自研命名**把价量算子注册进 ``CUSTOM_FACTORS``（幂等）。"""
    registered: list[str] = []
    for category, group in GROUPS.items():
        for name, (fn, window, unit) in group.items():
            factor_id = house_id(category, name)
            factor(factor_id, unit=unit, window=int(window), warmup=int(window))(fn)
            registered.append(factor_id)
    return registered

def lookup(factor_id: str) -> tuple:
    """按**自研 factor_id** 查 (fn, window, unit)。"""
    for category, group in GROUPS.items():
        for name, spec in group.items():
            if house_id(category, name) == factor_id:
                return spec
    raise KeyError(factor_id)

__all__ = [
    "ALL_NAMES",
    "CROSS_SECTION_ONLY",
    "GROUPS",
    "LIQUIDITY",
    "MOMENTUM",
    "REVERSAL",
    "RISK",
    "SIZE",
    "lookup",
    "register_price_ops",
]
