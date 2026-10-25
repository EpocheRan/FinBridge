"""Screener composition root。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_interface.common.config import get_config
from data_interface.market.loader import MarketDataLoader
from data_interface.market.sources.index_source import IndexMarketSource
from screener.application.backfill_service import BackfillService
from screener.application.baseline_service import BaselineService
from screener.application.build_orchestrator import BuildOrchestrator
from screener.application.factor_service import FactorService
from screener.application.screener_service import ScreenerService
from screener.config import ScreenerSettings
from screener.domain.factor_engine import FactorEngine
from screener.infrastructure.acquisition import (
    MarketDataAdapter,
    TradingCalendarAdapter,
)
from screener.infrastructure.job_queue import InProcessJobDispatcher
from screener.infrastructure.query_builder import QuestDBScreenerQuery
from screener.infrastructure.repository import (
    QuestDBBuildStateRepository,
    QuestDBFactorRepository,
    QuestDBSuspensionRepository,
)


@dataclass
class ScreenerApplication:
    settings: ScreenerSettings
    service: ScreenerService
    baseline_service: BaselineService
    orchestrator: BuildOrchestrator
    dispatcher: InProcessJobDispatcher
    build_state: QuestDBBuildStateRepository

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self) -> None:
        self.dispatcher.stop()


def build_screener_application(config: dict[str, Any] | None = None) -> ScreenerApplication:
    config = config or get_config()
    settings = ScreenerSettings.from_dict(config.get("screener", {}))

    loader = MarketDataLoader(config=config)
    fetcher = loader.fetcher
    db = fetcher.db
    calendar = loader.trading_calendar
    calendar_port = TradingCalendarAdapter(calendar)
    market = MarketDataAdapter(
        loader=loader,
        fetcher=fetcher,
        db=db,
        calendar=calendar,
        settings=settings,
        index_source=IndexMarketSource(settings=settings),
    )

    factor_store = QuestDBFactorRepository(db)
    build_state = QuestDBBuildStateRepository(db)
    suspension = QuestDBSuspensionRepository(db)
    query = QuestDBScreenerQuery(db)

    engine = FactorEngine(settings.daily)
    factor_service = FactorService(market, factor_store, engine)
    backfill = BackfillService(
        market=market,
        calendar=calendar_port,
        suspension=suspension,
        factor_service=factor_service,
        settings=settings.backfill,
    )
    orchestrator = BuildOrchestrator(
        build_state=build_state,
        backfill=backfill,
        factor_store=factor_store,
        market=market,
        calendar=calendar_port,
        settings=settings,
    )
    dispatcher = InProcessJobDispatcher(
        build_state=build_state,
        orchestrator=orchestrator,
        market=market,
        settings=settings,
    )
    service = ScreenerService(
        settings=settings,
        market=market,
        factor_store=factor_store,
        query=query,
        build_state=build_state,
        calendar=calendar_port,
        orchestrator=orchestrator,
        dispatcher=dispatcher,
    )
    baseline_service = BaselineService(
        settings=settings,
        factor_store=factor_store,
        calendar=calendar_port,
        orchestrator=orchestrator,
    )
    return ScreenerApplication(
        settings=settings,
        service=service,
        baseline_service=baseline_service,
        orchestrator=orchestrator,
        dispatcher=dispatcher,
        build_state=build_state,
    )
