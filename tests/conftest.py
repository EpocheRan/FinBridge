"""pytest 公共配置。

默认只跑**离线**测试：不联网、不依赖 QuestDB。
需要真实数据源/数据库的用例统一加 ``@require_live``，用
``RUN_LIVE_TESTS=1 pytest`` 显式打开。
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

LIVE_ENABLED = os.getenv("RUN_LIVE_TESTS") == "1"

require_live = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="需要真实数据源/QuestDB；设置 RUN_LIVE_TESTS=1 后运行",
)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 依赖真实网络或 QuestDB 的用例")


class StubLoader:
    """行情桩：给出稳定的 40 天日线，足够算 ATR。"""

    def __init__(self, last_close: float = 10.0, days: int = 40):
        self.last_close = last_close
        self.days = days
        self.calls: list[tuple] = []

    def ohlc(self, codes, start_date, end_date, freq="d", now=False, adjust="qfq"):
        if isinstance(codes, str):
            codes = [codes]
        self.calls.append((tuple(codes), start_date, end_date, freq, now, adjust))
        index = pd.date_range(end=date.today(), periods=self.days, freq="D")
        closes = [self.last_close - (self.days - 1 - i) * 0.1 for i in range(self.days)]
        frame = pd.DataFrame(
            {
                "open": closes,
                "high": [value + 0.2 for value in closes],
                "low": [value - 0.2 for value in closes],
                "close": closes,
                "raw_close": closes,
                "volume": [1000] * self.days,
                "amount": [10000.0] * self.days,
                "turnover": [1.0] * self.days,
                "adj_factor": [1.0] * self.days,
            },
            index=index,
        )
        frame.attrs.update({"adjust": adjust, "adjust_anchor": 1.0, "adjust_fallback": None})
        return {code: frame.copy() for code in codes}


@pytest.fixture
def sqlite_manager():
    """用 SQLite 承载 ORM，并把 ``_update`` 桥接到 SQLite，完全离线。"""
    from position import schema
    from position.manager import PositionManager

    engine = create_engine("sqlite:///:memory:")
    schema.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with patch("position.manager.QuestDBClient") as client_cls:
        client_cls.return_value.pd_conn = engine
        manager = PositionManager(data_loader=StubLoader())

    manager.Session = session_factory

    def _sqlite_update(sql: str, params):
        with engine.begin() as connection:
            # exec_driver_sql：把位置参数原样交给 DBAPI（SQLAlchemy 2.0 的
            # text() 不接受位置元组，会报 "List argument must consist only of dictionaries"）
            connection.exec_driver_sql(sql.replace("%s", "?"), tuple(params))
        return True

    manager._update = _sqlite_update
    manager.engine = engine
    try:
        yield manager
    finally:
        engine.dispose()


@pytest.fixture
def position_interface(sqlite_manager):
    from position.llm_tool import PositionAgentInterface

    interface = object.__new__(PositionAgentInterface)
    interface.manager = sqlite_manager
    return interface

