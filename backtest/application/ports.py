"""应用端口（Protocol）。

只定义抽象，具体实现由 infrastructure/bootstrap 注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Protocol, Sequence

import pandas as pd

from backtest.domain.models import (
    BacktestResult,
    DataBundle,
    ExecutionRules,
    OutputArtifact,
    RunSpec,
    TradingCalendar,
    Universe,
)


@dataclass(frozen=True)
class EngineRequest:
    spec: RunSpec
    data: DataBundle
    panel: Any
    strategy: Any
    universe: Universe
    calendar: TradingCalendar
    rules: ExecutionRules
    benchmark: pd.Series | None = None
    seed: int = 0
    cross_plan: Any | None = None
    corporate_actions: pd.DataFrame | None = None


class DataSourcePort(Protocol):
    name: ClassVar[str]

    def load(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        warmup: int = 0,
        freq: str = "d",
    ) -> DataBundle: ...


class UniversePort(Protocol):
    name: ClassVar[str]

    def resolve(self, as_of: date) -> Universe: ...


class FactorSourcePort(Protocol):
    name: ClassVar[str]

    def read(
        self,
        defs: Sequence[Any],
        codes: Sequence[str],
        start: date,
        end: date,
    ) -> Any: ...


class OutputPort(Protocol):
    name: ClassVar[str]

    def emit(self, result: BacktestResult) -> list[OutputArtifact]: ...


class EnginePort(Protocol):
    name: ClassVar[str]

    def run(self, request: EngineRequest) -> BacktestResult: ...
