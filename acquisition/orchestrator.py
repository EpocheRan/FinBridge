"""整合同步（acquisition）：Scope、数据集编排、进度输出。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Sequence

from data_interface.common.progress import (
    ConsoleProgressReporter,
    NullProgressReporter,
    ProgressReporter,
)


def _date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class Scope:
    start: date
    end: date
    codes: tuple[str, ...] | None = None
    boards: tuple[str, ...] | None = None
    datasets: tuple[str, ...] | None = None
    factor_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("Scope.start 不能晚于 end")
        if self.codes is not None:
            object.__setattr__(self, "codes", tuple(sorted({str(c) for c in self.codes})))
        if self.boards is not None:
            object.__setattr__(self, "boards", tuple(sorted({str(b) for b in self.boards})))
        if self.factor_ids is not None:
            object.__setattr__(
                self, "factor_ids", tuple(sorted({str(f) for f in self.factor_ids}))
            )


#: 数据集依赖顺序
DATASET_ORDER: tuple[str, ...] = (
    "trading_calendar",
    "stock_basic",
    "ohlcv_1d",
    "benchmark",
    "financial",
    "valuation",
    "moneyflow",
    "concept_meta",
    "concept_daily",
    "concept_member",
    "concept_class",
    "factors",
    "screener",
)

Syncer = Callable[[Scope], dict]


@dataclass
class Orchestrator:
    syncers: dict[str, Syncer]
    progress: ProgressReporter | None = None
    order: tuple[str, ...] = DATASET_ORDER
    continue_on_error: bool = True
    auto_continue: bool = False
    max_rounds: int = 50
    timeout_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.progress is None:
            self.progress = ConsoleProgressReporter()
        unknown = set(self.syncers) - set(self.order)
        if unknown:
            raise ValueError(f"未编排的数据集: {sorted(unknown)}")

    def run(self, scope: Scope) -> dict:
        selected = tuple(scope.datasets) if scope.datasets is not None else self.order
        unknown = [name for name in selected if name not in self.syncers]
        if unknown:
            raise ValueError(f"未知数据集: {unknown}")
        results: dict[str, dict] = {}
        errors: list[dict] = []
        print(f"[acquisition] start {scope.start}..{scope.end} datasets={list(selected)}")
        for name in selected:
            syncer = self.syncers[name]
            self.progress.start(name, 1, f"{scope.start}..{scope.end}")
            deadline = (
                time.monotonic() + self.timeout_seconds
                if self.timeout_seconds and self.timeout_seconds > 0
                else None
            )
            rounds = 0
            result: dict = {}
            try:
                while True:
                    result = syncer(scope)
                    rounds += 1
                    done = bool(result.get("done", True))
                    if done or not self.auto_continue:
                        break
                    if rounds >= self.max_rounds:
                        print(
                            f"[acquisition] {name}: 达到 max_rounds={self.max_rounds}，提前停止"
                        )
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        print(
                            f"[acquisition] {name}: 达到 timeout={self.timeout_seconds}s，提前停止"
                        )
                        break
                results[name] = result
                self.progress.finish(
                    name,
                    1,
                    f"rows={result.get('rows_written', '?')} rounds={rounds} "
                    f"done={result.get('done', True)}",
                )
            except Exception as exc:
                errors.append({"dataset": name, "error": f"{type(exc).__name__}: {exc}"})
                results[name] = {"error": str(exc)}
                self.progress.finish(name, 1, f"FAILED {exc}")
                if not self.continue_on_error:
                    raise
        return {"scope": scope, "results": results, "errors": errors}


def build_orchestrator(
    progress: ProgressReporter | None = None,
    *,
    auto_continue: bool = False,
    max_rounds: int = 50,
    timeout_seconds: float = 0.0,
) -> Orchestrator:
    from .syncers import REAL_SYNCERS

    return Orchestrator(
        syncers=REAL_SYNCERS,
        progress=progress,
        auto_continue=auto_continue,
        max_rounds=max_rounds,
        timeout_seconds=timeout_seconds,
    )
