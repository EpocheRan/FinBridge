"""Deprecated shim：实现已迁到 ``factors.technical.atr``。"""

from __future__ import annotations

from factors.technical.atr import ATR, atr, atr_wilder, true_range

__all__ = ["ATR", "atr", "atr_wilder", "true_range"]
