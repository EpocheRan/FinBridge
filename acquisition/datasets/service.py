"""统一调度入口：组合各数据集 mixin。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .benchmark import BenchmarkSyncMixin
from .concept import ConceptSyncMixin
from .daily import DailySyncMixin
from .financial import FinancialSyncMixin
from .moneyflow import MoneyflowSyncMixin
from .screener import ScreenerSyncMixin


class AShareSyncService(
    DailySyncMixin,
    FinancialSyncMixin,
    BenchmarkSyncMixin,
    ScreenerSyncMixin,
    MoneyflowSyncMixin,
    ConceptSyncMixin,
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
        self._concept_service: ConceptService | None = None

    def run(
        self,
        *,
        dataset: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
        codes: Sequence[str] | str | None = None,
        max_trade_days: int | None = None,
        max_stocks: int | None = None,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        include_funds: bool | None = None,
        include_benchmark: bool = True,
        include_screener: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """按阶段编排一次股票数据更新。

        - ``dataset="all"``：日线 → 基准 → 财务 → Screener → 分档 → 板块列表；
        - ``codes`` 为空：全市场；指定后只更新这些代码；
        - ``start_date`` / ``end_date``：日线/基准/Screener/分档用交易日，
          财务用报告年份 + 估值日期范围。
        """
        dataset = dataset.lower()
        if dataset not in DATASETS:
            raise ValueError(f"dataset 必须是 {' / '.join(DATASETS)}")

        selected_codes = normalize_selected_codes(codes)
        # 综合更新默认包含估值；财务单独调用时仍可显式关闭。
        if include_valuation is None:
            include_valuation = True

        result: dict[str, Any] = {
            "dataset": dataset,
            "codes": selected_codes,
            "start_date": start_date,
            "end_date": end_date,
        }

        need_app = (
            (dataset == "benchmark" and include_benchmark)
            or (dataset == "screener" and include_screener)
            or (dataset == "all" and (include_benchmark or include_screener))
        )
        app = self._get_screener_app() if need_app else None

        if dataset in ("daily", "all"):
            result["daily"] = self.sync_daily(
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                cursor=cursor if dataset == "daily" else None,
                reset=reset,
                include_funds=include_funds,
                codes=selected_codes,
            )
        if dataset in ("benchmark", "all"):
            if include_benchmark:
                result["benchmark"] = self.sync_benchmark(
                    start_date=start_date,
                    end_date=end_date,
                    app=app,
                )
            else:
                result["benchmark"] = {
                    "dataset": "benchmark",
                    "done": True,
                    "status": "SKIPPED",
                }
        if dataset in ("financial", "all"):
            result["financial"] = self.sync_financials(
                start_date=start_date,
                end_date=end_date,
                max_stocks=max_stocks,
                cursor=cursor if dataset == "financial" else None,
                reset=reset,
                include_valuation=include_valuation,
                codes=selected_codes,
                force=force,
            )
        if dataset in ("screener", "all"):
            if include_screener:
                result["screener"] = self.sync_screener(
                    start_date=start_date,
                    end_date=end_date,
                    codes=selected_codes,
                    force=force,
                    app=app,
                )
            else:
                result["screener"] = {
                    "dataset": "screener",
                    "done": True,
                    "status": "SKIPPED",
                }
        if dataset in ("moneyflow", "all"):
            result["moneyflow"] = self.sync_moneyflow(
                start_date=start_date,
                end_date=end_date,
                max_trade_days=max_trade_days,
                cursor=cursor if dataset == "moneyflow" else None,
                reset=reset,
            )
        if dataset in ("concept_meta", "all"):
            result["concept_meta"] = self.sync_concept_meta()
        if dataset == "concept_daily":
            result["concept_daily"] = self.sync_concept_daily(
                start_date=start_date,
                end_date=end_date,
                max_boards=max_boards,
                cursor=cursor,
                reset=reset,
            )
        if dataset == "concept_member":
            result["concept_member"] = self.sync_concept_member(
                max_boards=max_boards,
                cursor=cursor,
                reset=reset,
            )
        if dataset == "concept_class":
            result["concept_class"] = self.sync_concept_classification()

        phase_keys = [
            key
            for key in (
                "daily",
                "benchmark",
                "financial",
                "screener",
                "moneyflow",
                "concept_meta",
                "concept_daily",
                "concept_member",
                "concept_class",
            )
            if key in result
        ]
        result["done"] = all(result[key].get("done", True) for key in phase_keys)
        result["next_cursors"] = {
            key: result[key].get("next_cursor")
            for key in ("daily", "financial", "moneyflow", "concept_daily", "concept_member")
            if key in result
        }
        if dataset == "daily":
            result["next_cursor"] = result["daily"].get("next_cursor")
        elif dataset == "financial":
            result["next_cursor"] = result["financial"].get("next_cursor")
        elif dataset in ("moneyflow", "concept_daily", "concept_member"):
            result["next_cursor"] = result[dataset].get("next_cursor")
        elif dataset == "all":
            if not result["daily"].get("done"):
                result["next_cursor"] = result["daily"].get("next_cursor")
            else:
                result["next_cursor"] = result["financial"].get("next_cursor")
        else:
            result["next_cursor"] = None
        return result

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
            "concept_meta": latest.get(CONCEPT_META_STATE),
            "concept_daily": latest.get(CONCEPT_DAILY_STATE),
            "concept_member": latest.get(CONCEPT_MEMBER_STATE),
            "concept_class": latest.get(CONCEPT_CLASS_STATE),
        }
