"""财务/估值同步。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class FinancialSyncMixin:
    def sync_financials(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_stocks: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_valuation: bool | None = None,
        codes: Sequence[str] | str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        selected_codes = normalize_selected_codes(codes)
        state_dataset = (
            FINANCIAL_STATE if selected_codes is None else FINANCIAL_CODES_STATE
        )

        if start_date:
            start_day = date.fromisoformat(str(start_date)[:10])
            start_year = start_day.year
        else:
            if selected_codes is None:
                start_day = date(
                    int(self.settings.get("financial_start_year", 2020)), 1, 1
                )
            else:
                # 指定代码的首次财务更新默认最近 2 年，避免无意回补全历史。
                start_day = date(
                    max(1990, date.fromisoformat(today_str()).year - 1), 1, 1
                )
            start_year = start_day.year
        if end_date:
            end_day = date.fromisoformat(str(end_date)[:10])
            end_year = end_day.year
        else:
            today = date.fromisoformat(today_str())
            end_day = date(today.year, 12, 31)
            end_year = today.year
        if start_day > end_day:
            raise ValueError(f"start_date({start_day}) 不能晚于 end_date({end_day})")

        valuation_start = start_day.isoformat() if start_date else f"{start_year}-01-01"
        valuation_end = end_day.isoformat() if end_date else f"{end_year}-12-31"
        if max_stocks is None:
            max_stocks = (
                self.settings.get("financial_batch_stocks", 15)
                if selected_codes is None
                else self.settings.get("financial_selected_batch_stocks", 50)
            )
        max_stocks = max(1, int(max_stocks))
        include_valuation = (
            bool(self.settings.get("include_valuation", False))
            if include_valuation is None
            else include_valuation
        )
        payload = {
            "start_year": start_year,
            "end_year": end_year,
            "valuation_start": valuation_start,
            "valuation_end": valuation_end,
            "include_valuation": include_valuation,
            "codes": selected_codes,
        }

        previous = None if reset or force else self.state.get(TASK_NAME, state_dataset)
        if previous and previous.get("payload"):
            try:
                old = json.loads(previous["payload"])
            except Exception:
                old = {}
            # 兼容旧状态（没有 valuation_start/end/codes 字段）：
            # 旧任务按年份回填时，默认估值区间就是整年。
            old_valuation_start = old.get("valuation_start") or (
                f"{old.get('start_year')}-01-01"
                if old.get("start_year")
                else None
            )
            old_valuation_end = old.get("valuation_end") or (
                f"{old.get('end_year')}-12-31" if old.get("end_year") else None
            )
            if (
                old.get("start_year") != start_year
                or old.get("end_year") != end_year
                or old_valuation_start != valuation_start
                or old_valuation_end != valuation_end
                or bool(old.get("include_valuation")) != include_valuation
                or old.get("codes") != selected_codes
            ):
                previous = None

        if previous and previous.get("status") == "DONE" and not reset and not cursor:
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": previous.get("total") or 0,
                "processed": previous.get("processed") or 0,
                "next_cursor": None,
                "stocks_written": 0,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        skipped_codes: list[str] = []
        if selected_codes is None:
            universe = self.financial.ensure_stock_universe(
                int(self.settings.get("min_universe_size", 1000))
            )
            universe = sorted(code for code in universe if is_a_share_code(code))
        else:
            universe = []
            for code in selected_codes:
                if is_a_share_code(code):
                    universe.append(code)
                else:
                    skipped_codes.append(code)
            universe = sorted(set(universe))
        if not universe:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=0,
                processed=0,
                message="没有可同步的 A 股代码",
                payload=payload,
            )
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": 0,
                "processed": 0,
                "next_cursor": None,
                "stocks_written": 0,
                "skipped_codes": skipped_codes,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        current_cursor = cursor or (previous or {}).get("cursor")
        start_index = bisect_left(universe, current_cursor) if current_cursor else 0
        batch = universe[start_index : start_index + max_stocks]
        if not batch:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=len(universe),
                processed=len(universe),
                payload=payload,
            )
            return {
                "dataset": "financial",
                "codes": selected_codes,
                "done": True,
                "total": len(universe),
                "processed": len(universe),
                "next_cursor": None,
                "stocks_written": 0,
                "skipped_codes": skipped_codes,
                "errors": [],
                "valuation_range": [valuation_start, valuation_end],
            }

        errors: list[str] = []
        stocks_written = 0
        for offset, code in enumerate(batch, start=start_index):
            try:
                self.financial.sync_statements(
                    code, start_year=start_year, end_year=end_year, force=force
                )
                self.financial.sync_indicators(code)
                if include_valuation:
                    self.financial.sync_valuation(
                        code,
                        start_date=valuation_start,
                        end_date=valuation_end,
                        force=force,
                    )
                stocks_written += 1
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=code,
                    status="PAUSED",
                    total=len(universe),
                    processed=offset,
                    message="rate limit or permission",
                    payload=payload,
                )
                raise
            except Exception as exc:
                errors.append(f"{code}: {exc}")
                logger.warning("财务同步失败 %s: %s", code, exc)

        next_index = start_index + len(batch)
        next_cursor = universe[next_index] if next_index < len(universe) else None
        done = next_cursor is None
        self.state.save(
            TASK_NAME,
            state_dataset,
            cursor=next_cursor,
            status="DONE" if done else "RUNNING",
            total=len(universe),
            processed=next_index,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "financial",
            "codes": selected_codes,
            "done": done,
            "total": len(universe),
            "processed": next_index,
            "next_cursor": next_cursor,
            "stocks_written": stocks_written,
            "skipped_codes": skipped_codes,
            "valuation_range": [valuation_start, valuation_end],
            "errors": errors[:10],
        }

    # ------------------------------------------------------------------ 基准
