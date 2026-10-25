"""缺口识别与窗口计算（纯函数）。"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

from .models import Gap, GapType, merge_date_ranges


def required_lookback_bars(config) -> int:
    """计算 Beta / 52 周 / 长均线所需的最大历史交易日数。"""
    windows = [
        int(getattr(config, "beta_window", 252)),
        int(getattr(config, "high_low_window", 252)),
        int(getattr(config, "volatility_window", 20)),
    ]
    windows.extend(int(item) for item in getattr(config, "ma_windows", (5, 10, 20, 60, 120, 250)))
    return max(windows) + 5


def classify_gaps(
    *,
    code: str,
    expected_dates: Sequence[date],
    factor_dates: Iterable[date],
    raw_dates: Iterable[date],
    stale_dates: Iterable[date] = (),
    suspended_dates: Iterable[date] = (),
    delisted_dates: Iterable[date] = (),
    factor_version_mismatch: bool = False,
    lookback_short: bool = False,
    as_of: date | None = None,
) -> list[Gap]:
    """把一个 code 的日期缺口分类成若干 ``Gap``。

    优先级：DELISTED > SUSPENDED > FACTOR_STALE > FACTOR_MISSING > RAW_MISSING。
    """
    factor_set = set(factor_dates)
    raw_set = set(raw_dates)
    stale_set = set(stale_dates)
    suspended_set = set(suspended_dates)
    delisted_set = set(delisted_dates)

    buckets: dict[GapType, list[date]] = {}

    for day in expected_dates:
        if day in delisted_set:
            buckets.setdefault(GapType.DELISTED, []).append(day)
            continue
        if day in suspended_set:
            buckets.setdefault(GapType.SUSPENDED, []).append(day)
            continue
        if day in factor_set:
            if day in stale_set or factor_version_mismatch:
                buckets.setdefault(GapType.FACTOR_STALE, []).append(day)
            continue
        if day in raw_set:
            buckets.setdefault(GapType.FACTOR_MISSING, []).append(day)
        else:
            buckets.setdefault(GapType.RAW_MISSING, []).append(day)

    gaps = [
        Gap(code=code, dates=sorted(days), gap_type=gap_type)
        for gap_type, days in buckets.items()
        if days
    ]

    if lookback_short and as_of is not None:
        gaps.append(
            Gap(
                code=code,
                dates=[as_of],
                gap_type=GapType.LOOKBACK_SHORT,
                reason="历史窗口不足，部分因子按短窗口计算",
            )
        )
    return gaps


def summarize_missing(dates: Iterable[date]) -> list:
    return merge_date_ranges(list(dates))
