"""数据源适配器基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class FinancialSource(ABC):
    name: str

    @abstractmethod
    def fetch_profile(self, code: str) -> dict[str, Any] | None:
        """获取公司/证券基础信息。"""

    @abstractmethod
    def fetch_statements(
        self, code: str, *, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """返回 {"IS": [...], "BS": [...], "CF": [...]} 原始行。"""

    @abstractmethod
    def fetch_valuation(
        self, code: str, *, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """返回每日估值原始行。"""

    @abstractmethod
    def fetch_estimates(self, code: str) -> list[dict[str, Any]]:
        """返回一致预期原始行。"""
