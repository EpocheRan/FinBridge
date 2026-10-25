"""日频因子组合器（纯计算）。

输入长表 OHLCV、基准收益、股本参考数据，输出与 ``screener_daily``
对齐的 DataFrame。不读数据库、不发请求、不读配置文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import factors
from .errors import FactorComputationError


DAILY_COLUMNS = [
    "ts",
    "code",
    "open",
    "high",
    "low",
    "close",
    "raw_close",
    "adj_factor",
    "prev_close",
    "volume",
    "amount",
    "turnover",
    "pct_chg",
    "gap_pct",
    "intraday_pct",
    "range_pct",
    "avg_volume_5d",
    "avg_volume_20d",
    "avg_volume_60d",
    "avg_amount_20d",
    "volume_ratio_20d",
    "volume_chg_pct",
    "amount_ratio_20d",
    "atr14",
    "atr_pct",
    "volatility_20d",
    "volatility_60d",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "ma250",
    "close_vs_ma20_pct",
    "close_vs_ma60_pct",
    "close_vs_ma250_pct",
    "high_20d",
    "low_20d",
    "high_52w",
    "low_52w",
    "dist_high_52w_pct",
    "dist_low_52w_pct",
    "is_new_high_52w",
    "is_new_low_52w",
    "benchmark_code",
    "beta_252",
    "corr_252",
    "beta_window",
    "total_share",
    "float_share",
    "total_mv",
    "float_mv",
    "lookback_days",
    "is_partial",
    "core_ready",
    "full_ready",
]

_REQUIRED_RAW = {"ts", "code", "open", "high", "low", "close", "volume"}


@dataclass
class DailyFactorConfig:
    beta_window: int = 252
    beta_min_periods: int = 60
    volatility_window: int = 20
    volatility_60_window: int = 60
    atr_window: int = 14
    atr_method: str = "RMA"
    avg_volume_windows: tuple[int, ...] = (5, 20, 60)
    high_low_window: int = 252
    high_low_min_periods: int = 120
    ma_windows: tuple[int, ...] = (5, 10, 20, 60, 120, 250)
    benchmark_code: str = "000300"
    benchmark_market: str = "IDX"
    formula_version: str = "daily-v1"

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "DailyFactorConfig":
        payload = payload or {}
        return cls(
            beta_window=int(payload.get("beta_window", 252)),
            beta_min_periods=int(payload.get("beta_min_periods", 60)),
            volatility_window=int(payload.get("volatility_window", 20)),
            volatility_60_window=int(payload.get("volatility_60_window", 60)),
            atr_window=int(payload.get("atr_window", 14)),
            atr_method=str(payload.get("atr_method", "RMA")),
            avg_volume_windows=tuple(
                int(item) for item in payload.get("avg_volume_windows", (5, 20, 60))
            ),
            high_low_window=int(payload.get("high_low_window", 252)),
            high_low_min_periods=int(payload.get("high_low_min_periods", 120)),
            ma_windows=tuple(
                int(item) for item in payload.get("ma_windows", (5, 10, 20, 60, 120, 250))
            ),
            benchmark_code=str(payload.get("benchmark_code", "000300")),
            benchmark_market=str(payload.get("benchmark_market", "IDX")),
            formula_version=str(payload.get("formula_version", "daily-v1")),
        )

    @property
    def max_window(self) -> int:
        windows = [
            self.beta_window,
            self.high_low_window,
            self.volatility_window,
            self.volatility_60_window,
        ]
        windows.extend(self.ma_windows)
        windows.extend(self.avg_volume_windows)
        return max(windows)


def _normalize_ts(frame: pd.DataFrame, column: str = "ts") -> pd.DataFrame:
    result = frame.copy()
    if column not in result.columns:
        if isinstance(result.index, pd.DatetimeIndex):
            result = result.reset_index().rename(columns={"index": column})
        else:
            raise FactorComputationError(f"缺少 {column} 列")
    result[column] = pd.to_datetime(result[column], errors="coerce")
    try:
        if getattr(result[column].dt, "tz", None) is not None:
            result[column] = result[column].dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    return result


def _prepare_raw(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=list(_REQUIRED_RAW))
    frame = raw.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    missing = _REQUIRED_RAW - set(frame.columns)
    if missing:
        raise FactorComputationError(f"原始行情缺少列: {sorted(missing)}")
    frame = _normalize_ts(frame)
    frame["code"] = frame["code"].astype(str).str.split(".").str[0]
    for column in ("open", "high", "low", "close", "raw_close", "adj_factor", "volume", "amount", "turnover"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "amount" not in frame.columns:
        frame["amount"] = 0.0
    if "turnover" not in frame.columns:
        frame["turnover"] = 0.0
    frame = frame.dropna(subset=["ts", "code", "close"])
    frame = frame[frame["close"] > 0]
    return frame.sort_values(["code", "ts"]).reset_index(drop=True)


def _benchmark_returns(benchmark: pd.DataFrame | None) -> pd.Series:
    if benchmark is None or benchmark.empty:
        return pd.Series(dtype="float64")
    frame = benchmark.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if "close" not in frame.columns:
        return pd.Series(dtype="float64")
    frame = _normalize_ts(frame)
    frame = frame.dropna(subset=["ts", "close"]).sort_values("ts")
    frame = frame.drop_duplicates(subset=["ts"], keep="last")
    series = frame.set_index("ts")["close"].astype("float64")
    return factors.pct_change(series)


def _reference_row(reference: pd.DataFrame | None, code: str) -> dict[str, float]:
    if reference is None or reference.empty:
        return {}
    frame = reference.copy()
    if "code" not in frame.columns:
        if frame.index.name == "code":
            frame = frame.reset_index()
        else:
            return {}
    frame["code"] = frame["code"].astype(str).str.split(".").str[0]
    matched = frame[frame["code"] == code]
    if matched.empty:
        return {}
    row = matched.iloc[-1]
    return {
        "total_share": pd.to_numeric(row.get("total_share"), errors="coerce"),
        "float_share": pd.to_numeric(row.get("float_share"), errors="coerce"),
    }


def _compute_one(
    group: pd.DataFrame,
    *,
    bench_returns: pd.Series,
    reference: pd.DataFrame | None,
    config: DailyFactorConfig,
) -> pd.DataFrame:
    frame = group.sort_values("ts").reset_index(drop=True).copy()
    close = frame["close"].astype("float64")
    raw_close = (
        frame["raw_close"].astype("float64")
        if "raw_close" in frame.columns
        else close
    )
    frame["raw_close"] = raw_close
    if "adj_factor" not in frame.columns:
        frame["adj_factor"] = 1.0
    prev_close = close.shift(1)
    returns = factors.pct_change(close)

    frame["prev_close"] = prev_close
    frame["pct_chg"] = returns * 100.0
    frame["gap_pct"] = factors.gap_pct(frame["open"], prev_close) * 100.0
    frame["intraday_pct"] = factors.intraday_pct(close, frame["open"]) * 100.0
    frame["range_pct"] = factors.range_pct(frame["high"], frame["low"], prev_close) * 100.0

    volume = frame["volume"].astype("float64")
    amount = frame["amount"].astype("float64")
    frame["volume_chg_pct"] = factors.pct_change(volume) * 100.0

    for window in config.avg_volume_windows:
        frame[f"avg_volume_{window}d"] = factors.prior_mean(volume, window)
    frame["avg_amount_20d"] = factors.prior_mean(amount, 20)
    frame["volume_ratio_20d"] = factors._clean(
        volume.div(frame["avg_volume_20d"])
    )
    frame["amount_ratio_20d"] = factors._clean(
        amount.div(frame["avg_amount_20d"])
    )

    frame["atr14"] = factors.atr_wilder(
        frame["high"],
        frame["low"],
        close,
        config.atr_window,
    )
    frame["atr_pct"] = factors.atr_pct(frame["atr14"], close) * 100.0
    frame["volatility_20d"] = (
        factors.realized_volatility(
            returns, config.volatility_window
        )
        * 100.0
    )
    frame["volatility_60d"] = (
        factors.realized_volatility(
            returns, config.volatility_60_window
        )
        * 100.0
    )

    for window in config.ma_windows:
        frame[f"ma{window}"] = factors.rolling_mean(close, window)

    frame["close_vs_ma20_pct"] = (
        factors._clean(close.div(frame["ma20"]).sub(1.0)) * 100.0
        if "ma20" in frame
        else np.nan
    )
    frame["close_vs_ma60_pct"] = (
        factors._clean(close.div(frame["ma60"]).sub(1.0)) * 100.0
        if "ma60" in frame
        else np.nan
    )
    frame["close_vs_ma250_pct"] = (
        factors._clean(close.div(frame["ma250"]).sub(1.0)) * 100.0
        if "ma250" in frame
        else np.nan
    )

    frame["high_20d"] = factors.rolling_high(frame["high"], 20)
    frame["low_20d"] = factors.rolling_low(frame["low"], 20)
    frame["high_52w"] = factors.rolling_high(
        frame["high"],
        config.high_low_window,
        config.high_low_min_periods,
    )
    frame["low_52w"] = factors.rolling_low(
        frame["low"],
        config.high_low_window,
        config.high_low_min_periods,
    )
    frame["dist_high_52w_pct"] = (
        factors._clean(close.div(frame["high_52w"]).sub(1.0)) * 100.0
    )
    frame["dist_low_52w_pct"] = (
        factors._clean(close.div(frame["low_52w"]).sub(1.0)) * 100.0
    )
    frame["is_new_high_52w"] = (
        frame["high"].ge(frame["high_52w"]).fillna(False).astype(bool)
    )
    frame["is_new_low_52w"] = (
        frame["low"].le(frame["low_52w"]).fillna(False).astype(bool)
    )

    frame["bench_ret"] = frame["ts"].map(bench_returns)
    beta = factors.rolling_beta(
        returns,
        frame["bench_ret"],
        config.beta_window,
        config.beta_min_periods,
    )
    corr = factors.rolling_corr(
        returns,
        frame["bench_ret"],
        config.beta_window,
        config.beta_min_periods,
    )
    frame["beta_252"] = beta.reindex(frame.index)
    frame["corr_252"] = corr.reindex(frame.index)
    frame["benchmark_code"] = config.benchmark_code
    frame["beta_window"] = config.beta_window

    ref = _reference_row(reference, str(frame["code"].iloc[0]))
    frame["total_share"] = ref.get("total_share", np.nan)
    frame["float_share"] = ref.get("float_share", np.nan)
    frame["total_mv"] = factors.market_cap(raw_close, frame["total_share"])
    frame["float_mv"] = factors.market_cap(raw_close, frame["float_share"])

    fallback_turnover = factors._clean(
        volume.div(frame["float_share"]).mul(100.0)
    )
    frame["turnover"] = frame["turnover"].fillna(fallback_turnover)
    frame.loc[frame["turnover"] <= 0, "turnover"] = fallback_turnover

    frame["lookback_days"] = close.notna().cumsum().astype(int)
    frame["core_ready"] = (
        frame["open"].notna()
        & frame["high"].notna()
        & frame["low"].notna()
        & frame["close"].notna()
        & frame["pct_chg"].notna()
        & frame["gap_pct"].notna()
    ).astype(bool)
    required_full = [
        "high_52w",
        "low_52w",
        "atr14",
        "volatility_20d",
        "ma250",
    ]
    # 注意：beta_252 / corr_252 不能进 full_ready。
    # 基准指数是可选的交叉截面因子（基准没拉到、或指数源临时不可用时 beta 为
    # NaN），若把它当作"完整"的必要条件，则所有股票都会被标成 partial，
    # 而查询默认带 `is_partial = false`，筛股会直接返回空结果。
    full_ready = frame["lookback_days"] >= config.max_window
    for column in required_full:
        if column in frame.columns:
            full_ready &= frame[column].notna()
    frame["full_ready"] = full_ready.astype(bool)
    frame["is_partial"] = (~frame["full_ready"]).astype(bool)

    return frame


class FactorEngine:
    """把纯因子函数组合成 ``screener_daily`` 行。"""

    def __init__(self, config: DailyFactorConfig | None = None):
        self.config = config or DailyFactorConfig()

    def compute_daily(
        self,
        raw: pd.DataFrame,
        benchmark: pd.DataFrame | None = None,
        reference: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        frame = _prepare_raw(raw)
        if frame.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)

        bench_returns = _benchmark_returns(benchmark)
        outputs: list[pd.DataFrame] = []
        for code, group in frame.groupby("code", sort=True):
            try:
                outputs.append(
                    _compute_one(
                        group,
                        bench_returns=bench_returns,
                        reference=reference,
                        config=self.config,
                    )
                )
            except Exception as exc:
                raise FactorComputationError(
                    f"计算因子失败: {code}: {exc}"
                ) from exc

        if not outputs:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        result = pd.concat(outputs, ignore_index=True)
        for column in DAILY_COLUMNS:
            if column not in result.columns:
                result[column] = np.nan
        result = result[DAILY_COLUMNS].sort_values(["ts", "code"]).reset_index(drop=True)
        for column in result.columns:
            if result[column].dtype == object:
                continue
            if column in ("is_new_high_52w", "is_new_low_52w", "is_partial", "core_ready", "full_ready"):
                result[column] = result[column].fillna(False).astype(bool)
        return result
