"""因子计算引擎（factor_engine）。

职责：计算、写因子表、缓存、钩子、对账。
依赖：factors（纯库）+ data_interface（raw 数据）；不得 import screener/backtest/acquisition。
"""

from .audit import compare_frames
from .hooks import DATASET_FACTORS, FactorHooks
from .service import FactorEngineService
from .store import FactorStore

__all__ = [
    "DATASET_FACTORS",
    "FactorEngineService",
    "FactorHooks",
    "FactorStore",
    "compare_frames",
]
