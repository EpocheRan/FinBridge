"""统一异常体系。

MCP 工具层依赖这些异常生成结构化错误，避免把第三方 SDK 的原始异常
直接暴露给 Agent。
"""

from __future__ import annotations


class DataInterfaceError(Exception):
    """数据接口层基础异常。"""

    error_type = "DataInterfaceError"

    def __init__(self, message: str, *, source: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.source = source
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "source": self.source,
            "detail": self.detail,
        }


class ConfigurationError(DataInterfaceError):
    error_type = "ConfigurationError"


class SourceUnavailableError(DataInterfaceError):
    error_type = "SourceUnavailableError"


class DataNotFoundError(DataInterfaceError):
    error_type = "DataNotFoundError"


class PermissionDeniedError(DataInterfaceError):
    error_type = "PermissionDeniedError"

    def __init__(
        self,
        message: str,
        *,
        interface: str | None = None,
        required_points: int | None = None,
        source: str | None = None,
    ):
        super().__init__(message, source=source)
        self.interface = interface
        self.required_points = required_points

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "interface": self.interface,
                "required_points": self.required_points,
            }
        )
        return data


class RateLimitExceededError(DataInterfaceError):
    error_type = "RateLimitExceededError"

    def __init__(
        self,
        message: str,
        *,
        interface: str | None = None,
        limit: int | None = None,
        retry_after_seconds: int | None = None,
        source: str = "tushare",
    ):
        super().__init__(message, source=source)
        self.interface = interface
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "interface": self.interface,
                "limit": self.limit,
                "retry_after_seconds": self.retry_after_seconds,
            }
        )
        return data


class DailyQuotaExceededError(RateLimitExceededError):
    error_type = "DailyQuotaExceededError"

    def __init__(
        self,
        message: str,
        *,
        interface: str | None = None,
        limit: int | None = None,
        source: str = "tushare",
    ):
        super().__init__(
            message,
            interface=interface,
            limit=limit,
            retry_after_seconds=86400,
            source=source,
        )
