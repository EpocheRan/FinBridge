"""再平衡日期门控。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from strategies.universe import Rebalance


@dataclass(frozen=True)
class RebalanceSchedule:
    mode: Rebalance = Rebalance.WEEKLY
    every_n_bars: int = 1

    def __post_init__(self) -> None:
        if int(self.every_n_bars) <= 0:
            raise ValueError("every_n_bars 必须 > 0")

    @classmethod
    def from_value(cls, value: str | Rebalance, every_n_bars: int = 1) -> "RebalanceSchedule":
        if isinstance(value, Rebalance):
            mode = value
        else:
            text = str(value or "weekly").strip().lower()
            try:
                mode = Rebalance(text)
            except ValueError as exc:
                raise ValueError(f"未知再平衡模式: {value!r}") from exc
        return cls(mode=mode, every_n_bars=every_n_bars)

    def is_rebalance(self, dt: pd.Timestamp, prev_dt: pd.Timestamp | None, index: int) -> bool:
        dt = pd.Timestamp(dt).normalize()
        if self.mode == Rebalance.DAILY:
            return True
        if self.mode == Rebalance.EVERY_N_BARS:
            return index % self.every_n_bars == 0
        if self.mode == Rebalance.DRIFT:
            return False
        if prev_dt is None:
            return True
        prev_dt = pd.Timestamp(prev_dt).normalize()
        if self.mode == Rebalance.WEEKLY:
            return dt.isocalendar()[:2] != prev_dt.isocalendar()[:2]
        if self.mode == Rebalance.MONTHLY:
            return (dt.year, dt.month) != (prev_dt.year, prev_dt.month)
        return False

    def dates(self, calendar: Sequence[pd.Timestamp]) -> list[pd.Timestamp]:
        result = []
        prev = None
        for index, dt in enumerate(calendar):
            if self.is_rebalance(dt, prev, index):
                result.append(pd.Timestamp(dt).normalize())
            prev = dt
        return result
