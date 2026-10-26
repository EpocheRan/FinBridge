"""策略基类。

策略文件只定义模块级 ``STRATEGY``（类或工厂），loader 每次 run 实例化一次。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping, Sequence

import pandas as pd

from factors.library import FactorDef

from .context import PanelView, StrategyContext
from .universe import Rebalance, UniverseSpec


class BaseStrategy(ABC):
    name: ClassVar[str] = ""
    mode: ClassVar[str] = ""
    params: ClassVar[Mapping[str, Any]] = {}

    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = {**type(self).params, **dict(params or {})}
        self.validate()

    @abstractmethod
    def factors(self) -> Sequence[FactorDef]: ...

    def hyperparams(self) -> Sequence[Any]:
        return ()

    def validate(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} 缺少 name")
        if self.mode not in ("single", "cross"):
            raise ValueError(f"{type(self).__name__} 的 mode 必须是 single/cross")


class SingleSymbolStrategy(BaseStrategy):
    mode = "single"
    symbols: ClassVar[Sequence[str]] = ()

    def validate(self) -> None:
        super().validate()
        if not self.symbols:
            raise ValueError(f"{type(self).__name__} 缺少 symbols")

    def on_bar(self, ctx: StrategyContext) -> None:
        """默认不交易。"""
        return None


class CrossSectionStrategy(BaseStrategy):
    mode = "cross"
    universe: ClassVar[UniverseSpec] = UniverseSpec()
    top_n: ClassVar[int] = 30
    buffer: ClassVar[int] = 15
    rebalance: ClassVar[Rebalance] = Rebalance.WEEKLY
    weighting: ClassVar[Any] = None

    def validate(self) -> None:
        super().validate()
        if self.top_n <= 0:
            raise ValueError("top_n 必须 > 0")
        if self.buffer < 0:
            raise ValueError("buffer 不能为负")
        if self.universe.kind == "explicit" and not self.universe.codes:
            raise ValueError("explicit universe 不能为空")

    @abstractmethod
    def rank(self, panel: PanelView, dt: pd.Timestamp) -> pd.Series: ...

    def weights(self, panel: PanelView, scores: pd.Series, selected: Sequence[str]) -> dict[str, float]:
        from .weighting import WeightContext

        weighting = self.weighting
        if weighting is None:
            from .weighting import EqualWeight

            weighting = EqualWeight()
        return weighting.apply(
            WeightContext(
                panel=panel,
                scores=scores,
                selected=tuple(selected),
                dt=getattr(panel, "current_dt", None),
            )
        )

    def on_bar(self, ctx: StrategyContext) -> None:
        """可选：横截面策略的逐 bar 风控。"""
        return None
