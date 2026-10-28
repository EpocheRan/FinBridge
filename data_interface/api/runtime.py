"""进程内单例装配。

此前 ``MCPserver``、``market_tools``、``financial_tools``、``acquisition.api``
各自 new 了一份 ``MarketDataLoader`` / ``FinancialDataService`` / ``TradingCalendar``，
导致同进程里有多套 QuestDB 连接、多套 Tushare 限流器（实际配额可以被打到 2 倍）。
这里统一收口，所有入口共用同一份实例。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("DataInterface.Runtime")

_lock = threading.Lock()
_state: dict[str, Any] = {}
#: 每个 key 一把可重入锁：factory 内部可以安全地取其它单例（或同一个 key），
#: 不会因为持有全局锁而自锁。此前用全局 Lock + 在锁内调用 factory，
#: `get_trading_calendar()` / `get_position_interface()` 首次调用会永久挂起。
_creation_locks: dict[str, threading.RLock] = {}


def _get(key: str, factory):
    instance = _state.get(key)
    if instance is not None:
        return instance

    with _lock:
        key_lock = _creation_locks.setdefault(key, threading.RLock())

    with key_lock:
        instance = _state.get(key)
        if instance is None:
            instance = factory()
            with _lock:
                _state[key] = instance
            logger.info("初始化单例: %s", key)
        return instance


def get_market_loader():
    def _factory():
        from data_interface.market.loader import MarketDataLoader

        return MarketDataLoader()

    return _get("market_loader", _factory)


def get_trading_calendar():
    def _factory():
        return get_market_loader().trading_calendar

    return _get("trading_calendar", _factory)


def get_financial_service():
    def _factory():
        from data_interface.financial.service import FinancialDataService

        return FinancialDataService()

    return _get("financial_service", _factory)


def get_moneyflow_service():
    def _factory():
        from data_interface.moneyflow.service import MoneyflowService

        # 与行情/财务共用同一份 DB 连接与 Tushare 限流器
        return MoneyflowService()

    return _get("moneyflow_service", _factory)


def get_concept_service():
    def _factory():
        from data_interface.concept.service import ConceptService

        return ConceptService()

    return _get("concept_service", _factory)


def get_position_interface():
    def _factory():
        from position import PositionAgentInterface

        return PositionAgentInterface(data_loader=get_market_loader())

    return _get("position_interface", _factory)


def get_sync_service():
    def _factory():
        from acquisition.datasets import AShareSyncService

        # 与行情/财务工具共用同一份 FinancialDataService / TushareSource /
        # MarketDataFetcher / TradingCalendar，避免同进程多套限流器和连接。
        financial = get_financial_service()
        loader = get_market_loader()
        return AShareSyncService(
            db=financial.db,
            tushare=financial.tushare,
            financial=financial,
            market_fetcher=loader.fetcher,
            trading_calendar=loader.trading_calendar,
            adj_factors=loader.adj_factors,
        )

    return _get("sync_service", _factory)


def reset_singletons() -> None:
    """测试/优雅关闭用：尽量释放连接并清空所有单例。"""
    with _lock:
        for key, instance in _state.items():
            closers = []
            for name in ("close", "dispose"):
                closer = getattr(instance, name, None)
                if callable(closer):
                    closers.append(closer)
            # loader / 接口层本身没有 close，往下找一层持有连接的属性
            for attribute in ("fetcher", "db", "manager"):
                target = getattr(instance, attribute, None)
                for name in ("close", "dispose"):
                    closer = getattr(target, name, None)
                    if callable(closer):
                        closers.append(closer)
            for closer in closers:
                try:
                    closer()
                except Exception:
                    pass
        _state.clear()
        _creation_locks.clear()
        logger.info("已释放全部单例")
