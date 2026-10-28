"""跨模块事件协议（acquisition ↔ factor_engine 共用，无归属循环）。

事件类型放在 data_interface/common，避免 acquisition 与 factor_engine 互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


def _dates(values: Iterable[date | str] | None) -> tuple[date, ...]:
    result: list[date] = []
    for value in values or ():
        result.append(value if isinstance(value, date) else date.fromisoformat(str(value)[:10]))
    return tuple(sorted(set(result)))


def _codes(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item) for item in (values or ()) if str(item)}))


@dataclass(frozen=True)
class DataWritten:
    """某个 raw 数据集写完一批数据。"""

    dataset: str
    dates: tuple[date, ...] = ()
    codes: tuple[str, ...] = ()
    source: str | None = None
    source_version: str | None = None

    def __post_init__(self) -> None:
        dataset = str(self.dataset).strip()
        if not dataset:
            raise ValueError("DataWritten.dataset 不能为空")
        object.__setattr__(self, "dataset", dataset)
        object.__setattr__(self, "dates", _dates(self.dates))
        object.__setattr__(self, "codes", _codes(self.codes))

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "dates": [item.isoformat() for item in self.dates],
            "codes": list(self.codes),
            "source": self.source,
            "source_version": self.source_version,
        }
