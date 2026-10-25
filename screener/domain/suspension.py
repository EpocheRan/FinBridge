"""停牌 / 退市 / 无数据状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from screener.domain.clock import now_local


class SuspensionStatus(str, Enum):
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    NO_DATA = "NO_DATA"


_SEVERITY = {
    SuspensionStatus.SUSPENDED: 2,
    SuspensionStatus.DELISTED: 3,
    SuspensionStatus.NO_DATA: 1,
}


@dataclass
class Suspension:
    code: str
    market: str
    status: SuspensionStatus
    start_date: date
    end_date: date | None = None
    retry_at: datetime | None = None
    source: str | None = None
    reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def covers(self, day: date) -> bool:
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    def is_retry_blocked(self, now: datetime | None = None) -> bool:
        if self.status != SuspensionStatus.NO_DATA or self.retry_at is None:
            return False
        return (now or now_local()) < self.retry_at


def dominant_status(records: list[Suspension]) -> Suspension | None:
    """同一 code/date 命中多条时，按严重程度取一条。"""
    if not records:
        return None
    return max(records, key=lambda item: _SEVERITY.get(item.status, 0))
