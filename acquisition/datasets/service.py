"""数据集实现：组合各数据集 mixin。

对外编排入口是 ``acquisition.orchestrator``；``AShareSyncService`` 由
``acquisition.syncers`` 调用，只提供单个数据集的同步方法与状态查询。
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .benchmark import BenchmarkSyncMixin
from .concept import ConceptSyncMixin
from .daily import DailySyncMixin
from .financial import FinancialSyncMixin
from .moneyflow import MoneyflowSyncMixin
from .screener import ScreenerSyncMixin
from .sentiment import SentimentSyncMixin
from .valuation import ValuationSyncMixin


class AShareSyncService(
    DailySyncMixin,
    FinancialSyncMixin,
    BenchmarkSyncMixin,
    ScreenerSyncMixin,
    MoneyflowSyncMixin,
    SentimentSyncMixin,
    ConceptSyncMixin,
    ValuationSyncMixin,
):
    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        db: QuestDBClient | None = None,
        tushare: TushareSource | None = None,
        financial: FinancialDataService | None = None,
        state: TaskStateRepository | None = None,
        market_fetcher: MarketDataFetcher | None = None,
        trading_calendar: TradingCalendar | None = None,
        adj_factors: AdjFactorProvider | None = None,
        screener_app_factory: Callable[[], Any] | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("acquisition", {}) or self.config.get(
            "scheduled_tasks", {}
        )
        self.db = db or getattr(financial, "db", None) or QuestDBClient()
        self.tushare = tushare or getattr(financial, "tushare", None) or get_shared_tushare_source(self.config)
        self.financial = financial or FinancialDataService(
            db=self.db, tushare=self.tushare, config=self.config
        )
        self.market_fetcher = market_fetcher or MarketDataFetcher(
            "questdb", config=self.config, db=self.db
        )
        self.trading_calendar = trading_calendar or TradingCalendar(self.db)
        self.state = state or TaskStateRepository(self.db)
        self.adj_factors = adj_factors or AdjFactorProvider(
            self.config, tushare=self.tushare
        )
        self._screener_app_factory = screener_app_factory
        self._screener_app_instance: Any | None = None
        self._moneyflow_service: MoneyflowService | None = None
        self._sentiment_service: Any | None = None
        self._concept_service: ConceptService | None = None

    def status(self) -> dict[str, Any]:
        latest = self.state.list_latest(TASK_NAME)
        return {
            "daily": latest.get(DAILY_STATE),
            "daily_codes": latest.get(DAILY_CODES_STATE),
            "financial": latest.get(FINANCIAL_STATE),
            "financial_codes": latest.get(FINANCIAL_CODES_STATE),
            "benchmark": latest.get(BENCHMARK_STATE),
            "screener": latest.get(SCREENER_STATE),
            "moneyflow": latest.get(MONEYFLOW_STATE),
            "margin_detail": latest.get("margin_detail"),
            "top_list": latest.get("top_list"),
            "block_trade": latest.get("block_trade"),
            "valuation": latest.get(VALUATION_STATE),
            "concept_meta": latest.get(CONCEPT_META_STATE),
            "concept_daily": latest.get(CONCEPT_DAILY_STATE),
            "concept_member": latest.get(CONCEPT_MEMBER_STATE),
            "concept_class": latest.get(CONCEPT_CLASS_STATE),
        }
