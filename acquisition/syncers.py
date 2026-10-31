"""真实数据集适配：包一层 data_interface / acquisition.datasets / factor_engine。"""

from __future__ import annotations

from typing import Any

from .orchestrator import Scope

_service_instance: Any | None = None


def _service():
    global _service_instance
    if _service_instance is None:
        from data_interface.api.runtime import get_sync_service

        _service_instance = get_sync_service()
    return _service_instance


def daily(scope: Scope) -> dict:
    return _service().sync_daily(
        start_date=scope.start,
        end_date=scope.end,
        codes=list(scope.codes) if scope.codes else None,
    )


def benchmark(scope: Scope) -> dict:
    return _service().sync_benchmark(start_date=scope.start, end_date=scope.end)


def financial(scope: Scope) -> dict:
    return _service().sync_financials(
        start_date=scope.start,
        end_date=scope.end,
        codes=list(scope.codes) if scope.codes else None,
    )


def moneyflow(scope: Scope) -> dict:
    return _service().sync_moneyflow(
        start_date=scope.start, end_date=scope.end
    )


def margin_detail(scope: Scope) -> dict:
    return _service().sync_margin_detail(
        start_date=scope.start, end_date=scope.end
    )


def top_list(scope: Scope) -> dict:
    return _service().sync_top_list(
        start_date=scope.start, end_date=scope.end
    )


def block_trade(scope: Scope) -> dict:
    return _service().sync_block_trade(
        start_date=scope.start, end_date=scope.end
    )


def valuation(scope: Scope) -> dict:
    return _service().sync_valuation(
        start_date=scope.start, end_date=scope.end
    )


def concept_meta(scope: Scope) -> dict:
    return _service().sync_concept_meta()


def concept_daily(scope: Scope) -> dict:
    return _service().sync_concept_daily(
        start_date=scope.start,
        end_date=scope.end,
        max_boards=len(scope.boards) if scope.boards else None,
        board_codes=list(scope.boards) if scope.boards else None,
    )


def concept_member(scope: Scope) -> dict:
    return _service().sync_concept_member(
        max_boards=len(scope.boards) if scope.boards else None,
        board_codes=list(scope.boards) if scope.boards else None,
    )


def concept_class(scope: Scope) -> dict:
    return _service().sync_concept_classification()


def factors(scope: Scope) -> dict:
    from factor_engine.service import FactorEngineService

    service = FactorEngineService()
    ids = list(scope.factor_ids) if scope.factor_ids else service.default_precompute_ids()
    return service.build(
        ids,
        scope.start,
        scope.end,
        list(scope.codes) if scope.codes else None,
    )


def screener(scope: Scope) -> dict:
    return _service().sync_screener(
        start_date=scope.start,
        end_date=scope.end,
        codes=list(scope.codes) if scope.codes else None,
    )


def trading_calendar(scope: Scope) -> dict:
    # 日历由 TradingCalendar 读路径自动补齐；这里显式触发一次
    from data_interface.common.trading_calendar import TradingCalendar

    days = TradingCalendar().trading_days_or_weekdays(
        scope.start.isoformat(), scope.end.isoformat()
    )
    return {"dataset": "trading_calendar", "rows_written": len(days), "done": True}


def stock_basic(scope: Scope) -> dict:
    written = _service().financial.sync_stock_universe()
    return {"dataset": "stock_basic", "rows_written": int(written), "done": True}


REAL_SYNCERS = {
    "trading_calendar": trading_calendar,
    "stock_basic": stock_basic,
    "ohlcv_1d": daily,
    "benchmark": benchmark,
    "financial": financial,
    "valuation": valuation,
    "moneyflow": moneyflow,
    "margin_detail": margin_detail,
    "top_list": top_list,
    "block_trade": block_trade,
    "concept_meta": concept_meta,
    "concept_daily": concept_daily,
    "concept_member": concept_member,
    "concept_class": concept_class,
    "screener": screener,
    "factors": factors,
}
