"""行情数据子包。

注意：``__init__`` 保持轻量，避免仅导入 ``constants`` 时把 pandas/akshare
等重依赖一并加载（数据库初始化脚本只需要常量）。
"""

from __future__ import annotations

__all__ = ["MarketDataFetcher", "MarketDataLoader"]


def __getattr__(name: str):
    if name == "MarketDataFetcher":
        from .fetcher import MarketDataFetcher

        return MarketDataFetcher
    if name == "MarketDataLoader":
        from .loader import MarketDataLoader

        return MarketDataLoader
    raise AttributeError(name)
