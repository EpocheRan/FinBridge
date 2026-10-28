"""Deprecated shim：实现已迁到 ``factors.engine``。"""

from __future__ import annotations

from factors.engine import FactorEngine, IndicatorEngine

__all__ = ["FactorEngine", "IndicatorEngine"]
