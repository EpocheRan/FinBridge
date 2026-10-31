"""资金与情绪同步：两融 / 龙虎榜 / 大宗交易。

三个数据集共用 ``SentimentService``，按交易日批量拉取；每个数据集有独立
游标与 pending 重试状态（状态键就是数据集名）。
"""

from __future__ import annotations

import json
from typing import Any

from data_interface.sentiment.repository import SentimentRepository
from data_interface.sentiment.service import SentimentService

from .base import *  # noqa: F401,F403


class SentimentSyncMixin:
    def _get_sentiment_service(self) -> SentimentService:
        if self._sentiment_service is None:
            self._sentiment_service = SentimentService(
                config=self.config,
                repository=SentimentRepository(db=self.db, config=self.config),
                tushare=self.tushare,
                trading_calendar=self.trading_calendar,
            )
        return self._sentiment_service

    def _sync_sentiment(
        self,
        dataset: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        default_start = str(
            self.settings.get(
                "sentiment_start_date",
                self.settings.get("daily_start_date", "2020-01-01"),
            )
        )[:10]
        start = str(start_date or default_start)[:10]
        end = str(end_date or today_str())[:10]

        previous = None if reset else self.state.get(TASK_NAME, dataset)
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}

        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        attempts: dict[str, int] = dict(old_payload.get("attempts") or {})
        pending = [
            str(day)[:10]
            for day in (old_payload.get("pending") or [])
            if attempts.get(str(day)[:10], 0) < retry_limit
        ]

        current_cursor = cursor or (previous or {}).get("cursor")
        if not current_cursor and not reset:
            # 首次接入：从库内最新日期续跑，避免把默认 --start 2020 变成全历史回填。
            latest = self._get_sentiment_service().latest_ts(dataset)
            if latest is not None:
                start = max(start, latest.date().isoformat())
        if pending:
            # 待重试日期可能早于最新日期，不能被 start 截掉。
            start = min(start, min(pending))
        if start > end:
            start = end

        max_days = max(
            1, int(max_trade_days or self.settings.get("sentiment_batch_days", 10))
        )
        result = self._get_sentiment_service().sync(
            dataset,
            start,
            end,
            max_trade_days=max_days,
            cursor=current_cursor,
            pending=pending,
        )

        new_pending = [str(day)[:10] for day in (result.get("pending") or [])]
        for day in new_pending:
            attempts[day] = attempts.get(day, 0) + 1
        for day in list(attempts):
            if day not in new_pending:
                attempts.pop(day)
        effective_pending = [
            day for day in new_pending if attempts.get(day, 0) < retry_limit
        ]

        done = bool(result.get("done"))
        self.state.save(
            TASK_NAME,
            dataset,
            cursor=result.get("next_cursor"),
            status="DONE" if done else "RUNNING",
            total=int(result.get("total_days") or 0),
            processed=len(result.get("processed_days") or []),
            message=(
                f"rows={result.get('rows_written', 0)} "
                f"pending={len(effective_pending)}"
            ),
            payload={
                "start_date": start,
                "end_date": end,
                "pending": effective_pending,
                "attempts": attempts,
            },
        )
        result.update(
            {
                "start_date": start,
                "end_date": end,
                "pending": effective_pending,
            }
        )
        return result

    def sync_margin_detail(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        return self._sync_sentiment(
            "margin_detail",
            start_date=start_date,
            end_date=end_date,
            max_trade_days=max_trade_days,
            cursor=cursor,
            reset=reset,
        )

    def sync_top_list(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        return self._sync_sentiment(
            "top_list",
            start_date=start_date,
            end_date=end_date,
            max_trade_days=max_trade_days,
            cursor=cursor,
            reset=reset,
        )

    def sync_block_trade(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        return self._sync_sentiment(
            "block_trade",
            start_date=start_date,
            end_date=end_date,
            max_trade_days=max_trade_days,
            cursor=cursor,
            reset=reset,
        )
