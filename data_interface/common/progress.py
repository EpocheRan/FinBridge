"""进度协议（acquisition 实现，factor_engine 复用）。

共享类型放在 data_interface/common，避免 acquisition 与 factor_engine 互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProgressEvent:
    dataset: str
    phase: str  # start / update / finish
    done: int
    total: int
    current: str | None = None
    message: str | None = None

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 100.0
        return min(100.0, max(0.0, self.done * 100.0 / self.total))


class ProgressReporter(Protocol):
    def start(self, dataset: str, total: int, message: str | None = None) -> None: ...

    def update(self, event: ProgressEvent) -> None: ...

    def finish(self, dataset: str, total: int, message: str | None = None) -> None: ...


class NullProgressReporter:
    """测试/无 UI 环境使用。"""

    def start(self, dataset: str, total: int, message: str | None = None) -> None:
        return None

    def update(self, event: ProgressEvent) -> None:
        return None

    def finish(self, dataset: str, total: int, message: str | None = None) -> None:
        return None


class ConsoleProgressReporter:
    """控制台进度条；tqdm 不可用时退化为阶段打印。"""

    def __init__(self, *, min_interval_seconds: float = 0.2):
        self.min_interval_seconds = min_interval_seconds
        self._bars: dict[str, object] = {}
        self._last_print: dict[str, float] = {}

    def _tqdm(self, total: int, desc: str):
        try:
            from tqdm import tqdm

            return tqdm(total=total, desc=desc, leave=False, dynamic_ncols=True)
        except Exception:  # pragma: no cover - tqdm 是可选展示依赖
            return None

    def start(self, dataset: str, total: int, message: str | None = None) -> None:
        bar = self._tqdm(total, dataset)
        if bar is not None:
            self._bars[dataset] = bar
        elif message:
            print(f"[{dataset}] {message} ({total})")

    def update(self, event: ProgressEvent) -> None:
        bar = self._bars.get(event.dataset)
        if bar is not None:
            bar.n = min(event.done, event.total or event.done)
            if event.current:
                bar.set_postfix_str(str(event.current))
            bar.refresh()
            return
        print(
            f"[{event.dataset}] {event.done}/{event.total} "
            f"({event.percent:.1f}%)"
            + (f" {event.current}" if event.current else "")
        )

    def finish(self, dataset: str, total: int, message: str | None = None) -> None:
        bar = self._bars.pop(dataset, None)
        if bar is not None:
            bar.n = total
            bar.close()
        elif message:
            print(f"[{dataset}] done: {message}")
