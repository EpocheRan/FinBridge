"""兼容 shim：日频宽表计算已迁到 ``factor_engine.wide``。

screener 只负责筛选与构建编排；实际 56 列计算由 factor_engine 提供。
"""

from __future__ import annotations

from factor_engine.wide import DAILY_COLUMNS, DailyFactorConfig, FactorEngine

__all__ = ["DAILY_COLUMNS", "DailyFactorConfig", "FactorEngine"]
