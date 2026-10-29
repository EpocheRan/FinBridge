"""估值（按交易日全市场批量）同步。

一次 Tushare ``daily_basic`` 拉全市场，再用批量报表做 PIT 计算，
写入 ``fin_valuation_daily``，供 ``valuation.*`` 因子与 ``get_valuation`` 使用。
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class ValuationSyncMixin:
    def sync_valuation(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        default_start = str(
            self.settings.get("valuation_start_date", "2023-01-01")
        )[:10]
        start = max(str(start_date or default_start)[:10], default_start)
        end = str(end_date or today_str())[:10]
        if not reset:
            # 正常增量：从库内已覆盖的最后一天续跑，避免每次都从 2023 重新扫
            latest = self.financial.repository.get_valuation_latest_ts()
            if latest is not None:
                start = max(start, latest.date().isoformat())
        if start > end:
            start = end
        max_trade_days = max(
            1, int(max_trade_days or self.settings.get("valuation_batch_days", 5))
        )

        previous = None if reset else self.state.get(TASK_NAME, VALUATION_STATE)
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

        all_days = self.trading_calendar.trading_days_or_weekdays(start, end)
        days = [day for day in all_days if not current_cursor or day >= current_cursor]
        batch = days[:max_trade_days]
        rows_written = 0
        processed: list[str] = []
        errors: list[str] = []

        for day in batch:
            try:
                result = self.financial.sync_valuation_by_date(day)
                rows_written += int(result.get("rows_written") or 0)
                processed.append(day)
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    VALUATION_STATE,
                    cursor=day,
                    status="PAUSED",
                    total=len(all_days),
                    processed=len(processed),
                    message="rate limit or permission",
                    payload={"start_date": start, "end_date": end},
                )
                raise
            except Exception as exc:
                logger.warning("估值批量同步失败 %s: %s", day, exc)
                errors.append(f"{day}: {exc}")

        more = len(days) > len(batch)
        if batch:
            last_day = batch[-1]
            state_cursor = (
                days[len(batch)]
                if more
                else (date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
            )
        else:
            state_cursor = current_cursor
        done = not more
        self.state.save(
            TASK_NAME,
            VALUATION_STATE,
            cursor=state_cursor,
            status="DONE" if done else "RUNNING",
            total=len(all_days),
            processed=sum(
                1 for day in all_days if state_cursor and day < state_cursor
            ),
            message="; ".join(errors[:5]) if errors else None,
            payload={"start_date": start, "end_date": end},
        )
        return {
            "dataset": "valuation",
            "start_date": start,
            "end_date": end,
            "done": done,
            "rows_written": rows_written,
            "processed_days": processed,
            "next_cursor": days[len(batch)] if more else None,
            "errors": errors[:10],
        }
