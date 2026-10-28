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
    return service.build(
        None,
        scope.start,
        scope.end,
        list(scope.codes) if scope.codes else None,
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


#: 未实现的数据集先显式跳过，避免静默成功
def _not_implemented(name: str):
    def _run(scope: Scope) -> dict:
        return {"dataset": name, "skipped": True, "reason": "not implemented yet"}

    return _run


REAL_SYNCERS = {
    "trading_calendar": trading_calendar,
    "stock_basic": stock_basic,
    "ohlcv_1d": daily,
    "benchmark": benchmark,
    "financial": financial,
    "valuation": _not_implemented("valuation"),
    "moneyflow": moneyflow,
    "concept_meta": concept_meta,
    "concept_daily": concept_daily,
    "concept_member": concept_member,
    "concept_class": concept_class,
    "factors": factors,
}
