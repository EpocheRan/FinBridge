"""Screener 限界上下文。

对外只暴露 bootstrap 构建的 application 容器；内部按
``domain -> application -> infrastructure -> interface`` 分层。
"""

from __future__ import annotations

__all__ = ["ScreenerApplication"]


def __getattr__(name: str):
    if name == "ScreenerApplication":
        from .bootstrap import ScreenerApplication

        return ScreenerApplication
    raise AttributeError(name)
