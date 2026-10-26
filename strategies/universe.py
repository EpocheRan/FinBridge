"""策略侧 universe / 再平衡声明。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence


class Rebalance(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVERY_N_BARS = "every_n_bars"
    DRIFT = "drift"


@dataclass(frozen=True)
class UniverseSpec:
    kind: str = "explicit"
    codes: tuple[str, ...] = ()
    exclude_st: bool = True
    min_bars: int = 0
    as_of: date | None = None

    @classmethod
    def explicit(cls, codes: Sequence[str], *, min_bars: int = 0) -> "UniverseSpec":
        return cls(kind="explicit", codes=tuple(sorted(set(str(code) for code in codes))), min_bars=min_bars)

    @classmethod
    def all_a(cls, *, exclude_st: bool = True, min_bars: int = 0) -> "UniverseSpec":
        return cls(kind="all_a", exclude_st=exclude_st, min_bars=min_bars)

    @classmethod
    def index(cls, code: str, *, min_bars: int = 0) -> "UniverseSpec":
        return cls(kind="index", codes=(str(code),), min_bars=min_bars)

    def fingerprint(self) -> str:
        import hashlib

        payload = f"{self.kind}|{','.join(self.codes)}|{int(self.exclude_st)}|{self.min_bars}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 兼容设计文档里的 Universe.all_a(...) 写法
class Universe:
    all_a = staticmethod(UniverseSpec.all_a)
    explicit = staticmethod(UniverseSpec.explicit)
    index = staticmethod(UniverseSpec.index)
