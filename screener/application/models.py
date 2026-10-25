"""Application 内部结果对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from screener.domain.models import CoverageReport


@dataclass
class ChunkResult:
    codes: list[str]
    start: date
    end: date
    rows_written: int = 0
    coverage: CoverageReport | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ComputeResult:
    rows_written: int
    frame: object | None = None
