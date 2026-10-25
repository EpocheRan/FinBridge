"""分时量能基线（纯聚合）。

输入最近 N 个交易日的 1m/5m bar，输出 ``intraday_baseline`` 行。
不负责取数、写库或状态管理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .errors import FactorComputationError


BASELINE_COLUMNS = [
    "code",
    "freq",
    "minute_end",
    "session",
    "avg_volume",
    "avg_cum_volume",
    "avg_amount",
    "avg_cum_amount",
    "std_cum_volume",
    "sample_days",
    "expected_days",
    "coverage_ratio",
    "includes_auction",
    "is_partial",
    "source_start",
    "source_end",
]


@dataclass
class BaselineConfig:
    freq: str = "5m"
    lookback_days: int = 20
    min_sample_days: int = 5
    max_missing_bucket_ratio: float = 0.1
    max_age_days: int = 7
    formula_version: str = "baseline-v1"

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "BaselineConfig":
        payload = payload or {}
        return cls(
            freq=str(payload.get("freq", "5m")),
            lookback_days=int(payload.get("lookback_days", 20)),
            min_sample_days=int(payload.get("min_sample_days", 5)),
            max_missing_bucket_ratio=float(
                payload.get("max_missing_bucket_ratio", 0.1)
            ),
            max_age_days=int(payload.get("max_age_days", 7)),
            formula_version=str(payload.get("formula_version", "baseline-v1")),
        )


def freq_minutes(freq: str) -> int:
    value = str(freq).lower().strip()
    if not value.endswith("m"):
        raise FactorComputationError(f"不支持的基线频率: {freq}")
    try:
        return int(value[:-1])
    except ValueError as exc:
        raise FactorComputationError(f"不支持的基线频率: {freq}") from exc


def expected_buckets(freq: str = "5m") -> list[int]:
    """常规交易时段的桶结束分钟列表（距 00:00）。"""
    step = freq_minutes(freq)
    morning = list(range(570 + step, 690 + 1, step))
    afternoon = list(range(780 + step, 900 + 1, step))
    return morning + afternoon


def bucket_for_minute(minute: int, freq: str = "5m") -> int | None:
    step = freq_minutes(freq)
    if 570 <= minute <= 690:
        start = 570
    elif 780 <= minute <= 900:
        start = 780
    else:
        return None
    steps = max(1, int(np.ceil((minute - start) / step)))
    bucket = start + steps * step
    return bucket if bucket <= (690 if start == 570 else 900) else None


def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if "ts" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index().rename(columns={"index": "ts"})
        else:
            raise FactorComputationError("分时数据缺少 ts 列")
    required = {"code", "ts", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise FactorComputationError(f"分时数据缺少列: {sorted(missing)}")
    frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
    try:
        if getattr(frame["ts"].dt, "tz", None) is not None:
            frame["ts"] = frame["ts"].dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    if "amount" not in frame.columns:
        frame["amount"] = 0.0
    frame["code"] = frame["code"].astype(str).str.split(".").str[0]
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    frame = frame.dropna(subset=["ts", "code"])
    frame["trade_date"] = frame["ts"].dt.normalize()
    frame["minute"] = frame["ts"].dt.hour * 60 + frame["ts"].dt.minute
    return frame


def build_profile(
    bars: pd.DataFrame,
    config: BaselineConfig | None = None,
) -> pd.DataFrame:
    config = config or BaselineConfig()
    if bars is None or bars.empty:
        return pd.DataFrame(columns=BASELINE_COLUMNS)

    frame = _normalize(bars)
    frame["bucket"] = frame["minute"].map(
        lambda minute: bucket_for_minute(int(minute), config.freq)
    )
    frame = frame.dropna(subset=["bucket"])
    frame["bucket"] = frame["bucket"].astype(int)
    if frame.empty:
        return pd.DataFrame(columns=BASELINE_COLUMNS)

    aggregated = (
        frame.groupby(["code", "trade_date", "bucket"], as_index=False)
        .agg(volume=("volume", "sum"), amount=("amount", "sum"))
    )
    codes = sorted(aggregated["code"].unique())
    dates = sorted(aggregated["trade_date"].unique())
    buckets = expected_buckets(config.freq)
    if not buckets:
        return pd.DataFrame(columns=BASELINE_COLUMNS)

    full_index = pd.MultiIndex.from_product(
        [codes, dates, buckets], names=["code", "trade_date", "bucket"]
    )
    expanded = (
        aggregated.set_index(["code", "trade_date", "bucket"])
        .reindex(full_index)
        .reset_index()
    )
    expanded["present"] = expanded["volume"].notna()
    expanded["missing_ratio"] = 1.0 - expanded.groupby(
        ["code", "trade_date"]
    )["present"].transform("mean")
    expanded["volume"] = expanded["volume"].fillna(0.0)
    expanded["amount"] = expanded["amount"].fillna(0.0)
    expanded["cum_volume"] = expanded.groupby(["code", "trade_date"])[
        "volume"
    ].cumsum()
    expanded["cum_amount"] = expanded.groupby(["code", "trade_date"])[
        "amount"
    ].cumsum()

    valid = expanded[
        expanded["missing_ratio"] <= config.max_missing_bucket_ratio
    ].copy()
    if valid.empty:
        return pd.DataFrame(columns=BASELINE_COLUMNS)

    grouped = (
        valid.groupby(["code", "bucket"], as_index=False)
        .agg(
            avg_volume=("volume", "mean"),
            avg_cum_volume=("cum_volume", "mean"),
            avg_amount=("amount", "mean"),
            avg_cum_amount=("cum_amount", "mean"),
            std_cum_volume=("cum_volume", lambda values: values.std(ddof=1)),
            sample_days=("trade_date", "nunique"),
            source_start=("trade_date", "min"),
            source_end=("trade_date", "max"),
        )
    )
    grouped["std_cum_volume"] = grouped["std_cum_volume"].fillna(0.0)
    grouped["expected_days"] = int(config.lookback_days)
    grouped["coverage_ratio"] = (
        grouped["sample_days"] / max(1, int(config.lookback_days))
    ).clip(upper=1.0)
    grouped["is_partial"] = grouped["sample_days"] < int(config.min_sample_days)
    grouped["includes_auction"] = grouped["bucket"] == buckets[0]
    grouped["freq"] = config.freq
    grouped["minute_end"] = grouped["bucket"].astype(int)
    grouped["session"] = np.where(grouped["minute_end"] <= 690, "AM", "PM")
    grouped = grouped.sort_values(["code", "minute_end"]).reset_index(drop=True)
    for column in BASELINE_COLUMNS:
        if column not in grouped.columns:
            grouped[column] = np.nan
    return grouped[BASELINE_COLUMNS]
