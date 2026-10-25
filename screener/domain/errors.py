"""Screener 领域异常。

这些异常只表达业务语义，不依赖 FastMCP / QuestDB / 第三方 SDK。
interface 层负责把它们映射成结构化错误响应。
"""

from __future__ import annotations


class ScreenerError(Exception):
    error_type = "ScreenerError"

    def __init__(self, message: str, *, detail: str | None = None, **extra):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "detail": self.detail,
            **self.extra,
        }


class InvalidScreenSpec(ScreenerError):
    error_type = "InvalidScreenSpec"


class ScreenerNotBuilt(ScreenerError):
    error_type = "ScreenerNotBuilt"


class InsufficientHistory(ScreenerError):
    error_type = "InsufficientHistory"


class FactorComputationError(ScreenerError):
    error_type = "FactorComputationError"


class CoverageIncomplete(ScreenerError):
    error_type = "CoverageIncomplete"
