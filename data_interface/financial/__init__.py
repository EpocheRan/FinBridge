"""财务数据子包。

``__init__`` 保持轻量，避免初始化脚本导入字段字典时加载 pandas/tushare。
"""

from __future__ import annotations

__all__ = ["FinancialDataService", "FinancialRepository"]


def __getattr__(name: str):
    if name == "FinancialRepository":
        from .repository import FinancialRepository

        return FinancialRepository
    if name == "FinancialDataService":
        from .service import FinancialDataService

        return FinancialDataService
    raise AttributeError(name)
