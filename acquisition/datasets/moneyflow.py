"""资金流同步。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class MoneyflowSyncMixin:
    def _get_moneyflow_service(self) -> MoneyflowService:
        if self._moneyflow_service is None:
            self._moneyflow_service = MoneyflowService(
                config=self.config,
                repository=MoneyflowRepository(db=self.db, config=self.config),
                sources=[
                    TushareMoneyflowSource(self.config, tushare=self.tushare)
                ],
            )
        return self._moneyflow_service

    def sync_moneyflow(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """按交易日回补/增量分档数据。"""
        start = str(
            start_date
            or self.settings.get(
                "moneyflow_start_date",
                self.settings.get("daily_start_date", "2020-01-01"),
            )
        )[:10]
        end = str(end_date or today_str())[:10]
        previous = None if reset else self.state.get(TASK_NAME, MONEYFLOW_STATE)
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}
        if old_payload.get("start_date") and old_payload["start_date"] != start:
            previous = None
            old_payload = {}
        current_cursor = cursor or (previous or {}).get("cursor")
        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        attempts: dict[str, int] = dict(old_payload.get("attempts") or {})
        pending_days = [
            day
            for day in (old_payload.get("pending") or [])
            if attempts.get(day, 0) < retry_limit
        ]

        result = self._get_moneyflow_service().sync(
            start,
            end,
            max_trade_days=max_trade_days or 20,
            cursor=current_cursor,
            pending=pending_days,
        )
        new_pending = list(result.get("pending") or [])
        for day in new_pending:
            attempts[day] = attempts.get(day, 0) + 1
        for day in list(attempts):
            if day not in new_pending:
                attempts.pop(day)
        self.state.save(
            TASK_NAME,
            MONEYFLOW_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("total_days") or 0),
            processed=len(result.get("processed_days") or []),
            message=f"rows={result.get('rows_written', 0)} pending={len(new_pending)}",
            payload={
                "start_date": start,
                "end_date": end,
                "pending": new_pending,
                "attempts": attempts,
            },
        )
        result.update({"start_date": start, "end_date": end})
        return result

    # ------------------------------------------------------------------ 概念
