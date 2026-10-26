"""组合权重。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class WeightContext:
    panel: Any
    scores: pd.Series
    selected: Sequence[str]
    dt: pd.Timestamp | None = None
    gross_exposure: float = 1.0
    max_weight: float = 1.0


class Weighting(Protocol):
    def apply(self, context: WeightContext) -> dict[str, float]: ...


def _normalize(
    raw: dict[str, float],
    *,
    gross_exposure: float,
    max_weight: float,
) -> dict[str, float]:
    values = {
        symbol: float(value)
        for symbol, value in raw.items()
        if np.isfinite(value) and value > 0
    }
    if not values:
        return {}
    target = min(max(float(gross_exposure), 0.0), 1.0)
    total = sum(values.values())
    weights = {symbol: value / total * target for symbol, value in values.items()}
    cap = max(0.0, min(float(max_weight), 1.0))
    for _ in range(20):
        over = {symbol: value for symbol, value in weights.items() if value > cap + 1e-12}
        if not over:
            break
        excess = sum(value - cap for value in over.values())
        for symbol in over:
            weights[symbol] = cap
        free = [symbol for symbol, value in weights.items() if value < cap - 1e-12]
        if not free:
            break
        free_total = sum(weights[symbol] for symbol in free)
        if free_total <= 0:
            share = excess / len(free)
            for symbol in free:
                weights[symbol] += share
        else:
            for symbol in free:
                weights[symbol] += excess * (weights[symbol] / free_total)
    total = sum(weights.values())
    if total > target and total > 0:
        weights = {symbol: value * target / total for symbol, value in weights.items()}
    return weights


class EqualWeight:
    def apply(self, context: WeightContext) -> dict[str, float]:
        selected = [symbol for symbol in context.selected]
        if not selected:
            return {}
        raw = {symbol: 1.0 for symbol in selected}
        return _normalize(
            raw,
            gross_exposure=context.gross_exposure,
            max_weight=context.max_weight,
        )


class ScoreWeight:
    def apply(self, context: WeightContext) -> dict[str, float]:
        selected = [symbol for symbol in context.selected]
        scores = context.scores.reindex(selected).astype("float64")
        scores = scores.where(np.isfinite(scores))
        if scores.dropna().empty:
            return EqualWeight().apply(context)
        shifted = scores - scores.min()
        if float(shifted.sum()) <= 0:
            return EqualWeight().apply(context)
        raw = shifted.to_dict()
        return _normalize(
            raw,
            gross_exposure=context.gross_exposure,
            max_weight=context.max_weight,
        )


class InverseVolWeight:
    def __init__(self, factor_name: str = "vol20", cap: float = 0.08):
        self.factor_name = factor_name
        self.cap = cap

    def apply(self, context: WeightContext) -> dict[str, float]:
        if context.dt is None:
            return EqualWeight().apply(context)
        try:
            vol = context.panel.at(context.dt, self.factor_name)
        except Exception:
            return EqualWeight().apply(context)
        vol = vol.reindex(list(context.selected)).astype("float64")
        vol = vol.where(np.isfinite(vol) & (vol > 0))
        if vol.dropna().empty:
            return EqualWeight().apply(context)
        raw = (1.0 / vol).dropna().to_dict()
        return _normalize(
            raw,
            gross_exposure=context.gross_exposure,
            max_weight=min(float(self.cap), context.max_weight),
        )


class CappedWeight:
    def __init__(self, cap: float = 0.1):
        self.cap = cap

    def apply(self, context: WeightContext) -> dict[str, float]:
        selected = [symbol for symbol in context.selected]
        if not selected:
            return {}
        raw = {symbol: 1.0 for symbol in selected}
        return _normalize(
            raw,
            gross_exposure=context.gross_exposure,
            max_weight=min(float(self.cap), context.max_weight),
        )
