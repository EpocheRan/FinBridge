"""日线同步（含 ETF/LOF、复权因子、空数据日重试）。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class DailySyncMixin:
    def sync_daily(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trade_days: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        include_funds: bool | None = None,
        codes: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        selected_codes = normalize_selected_codes(codes)
        selected_set = set(selected_codes or ())
        state_dataset = DAILY_STATE if selected_codes is None else DAILY_CODES_STATE

        if include_funds is None:
            if selected_codes is None:
                include_funds = bool(self.settings.get("include_funds", True))
            else:
                # 指定代码时只为基金代码额外拉 ETF/LOF，避免无意义的全市场基金查询
                include_funds = any(
                    ticker_type(code) in ("etf", "lof", "fund")
                    for code in selected_codes
                )
        else:
            include_funds = bool(include_funds)

        if start_date is None:
            if selected_codes is None:
                start_date = self.settings.get("daily_start_date", "2020-01-01")
            else:
                # 指定代码的首次更新默认只看最近约 400 天，避免误触发全历史回填；
                # 需要完整历史时显式传 start_date。
                start_date = (
                    date.fromisoformat(today_str()) - timedelta(days=400)
                ).isoformat()
        start_date = str(start_date)[:10]
        end_date = str(end_date or today_str())[:10]
        if start_date > end_date:
            raise ValueError(f"start_date({start_date}) 不能晚于 end_date({end_date})")
        max_trade_days = max(1, int(max_trade_days or self.settings.get("daily_batch_days", 20)))
        retry_limit = max(1, int(self.settings.get("empty_day_retry_limit", 3)))
        payload: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "include_funds": include_funds,
            "codes": selected_codes,
        }

        previous = None if reset else self.state.get(TASK_NAME, state_dataset)
        old_payload: dict[str, Any] = {}
        if previous and previous.get("payload"):
            try:
                old_payload = json.loads(previous["payload"])
            except Exception:
                old_payload = {}
        # 起始日期、基金口径或代码清单变化视为新任务
        if old_payload and (
            old_payload.get("start_date") != start_date
            or bool(old_payload.get("include_funds")) != include_funds
            or old_payload.get("codes") != selected_codes
        ):
            previous = None
            old_payload = {}

        attempts: dict[str, int] = dict(old_payload.get("attempts") or {})
        pending: list[str] = list(old_payload.get("pending") or [])
        payload["attempts"] = attempts
        payload["pending"] = pending

        # 日历不可用/未覆盖时必须回退工作日；直接 get_trading_days 会把
        # “日历拉取失败”伪装成“区间内没有交易日”，任务静默 0 行。
        all_days = self.trading_calendar.trading_days_or_weekdays(start_date, end_date)
        if not all_days:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=None,
                status="DONE",
                total=0,
                processed=0,
                payload=payload,
            )
            return {
                "dataset": "daily",
                "codes": selected_codes,
                "done": True,
                "total": 0,
                "processed": 0,
                "next_cursor": None,
                "rows_written": 0,
                "fund_rows_written": 0,
                "errors": [],
            }

        current_cursor = cursor or (previous or {}).get("cursor")
        # 有 pending（上次没拿到的交易日）时，从最近的那个 pending 重试
        if not cursor and pending:
            earliest_pending = min(pending)
            current_cursor = min(current_cursor, earliest_pending) if current_cursor else earliest_pending

        days = [day for day in all_days if not current_cursor or day >= current_cursor]
        if not days:
            self.state.save(
                TASK_NAME,
                state_dataset,
                cursor=current_cursor,
                status="DONE",
                total=len(all_days),
                processed=len(all_days),
                payload=payload,
            )
            return {
                "dataset": "daily",
                "codes": selected_codes,
                "done": True,
                "total": len(all_days),
                "processed": len(all_days),
                "next_cursor": None,
                "rows_written": 0,
                "fund_rows_written": 0,
                "errors": [],
            }

        batch = days[:max_trade_days]
        rows_written = 0
        fund_rows_written = 0
        errors: list[str] = []
        paused_at: str | None = None

        # 指定代码时不重建全市场基金池，避免一次少量更新触发全量基金同步。
        if include_funds and selected_codes is None:
            try:
                self._ensure_fund_universe()
            except STOP_ERRORS:
                raise
            except Exception as exc:
                logger.warning("基金基础信息同步失败（继续拉日线）: %s", exc)

        for day in batch:
            trade_date = day.replace("-", "")
            try:
                raw = self.tushare.fetch_daily_all(trade_date)
                basic = self.tushare.fetch_daily_basic_all(trade_date) if raw else []
                if not raw:
                    should_pause = self._handle_empty_day(
                        day, attempts, pending, errors, retry_limit
                    )
                    if should_pause:
                        paused_at = day
                        break
                    continue

                frame = self._daily_frame(raw, basic)
                if selected_codes is not None and not frame.empty:
                    frame = frame[frame["code"].isin(selected_set)]
                frame = self._attach_factors(frame, trade_date)
                if not frame.empty:
                    rows_written += self.market_fetcher.update_ohlc(frame, "d")

                if include_funds:
                    fund_frame = self._fund_daily_frame(
                        self.tushare.fetch_fund_daily_all(trade_date)
                    )
                    if selected_codes is not None and not fund_frame.empty:
                        fund_frame = fund_frame[fund_frame["code"].isin(selected_set)]
                    if not fund_frame.empty:
                        fund_frame = self._attach_factors(fund_frame, trade_date)
                        # 股票/基金分开写：混在一起会让 rows_written 把基金也算成股票，
                        # 且 pandas 对「含全 NA 列的 concat」会告警
                        fund_rows_written += self.market_fetcher.update_ohlc(
                            fund_frame, "d"
                        )
                attempts.pop(day, None)
                if day in pending:
                    pending.remove(day)
            except STOP_ERRORS:
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=day,
                    status="PAUSED",
                    total=len(all_days),
                    processed=sum(1 for item in all_days if item < day),
                    message="rate limit or permission",
                    payload=payload,
                )
                raise
            except Exception as exc:
                logger.warning("日线批量同步失败 %s: %s", day, exc)
                self.state.save(
                    TASK_NAME,
                    state_dataset,
                    cursor=day,
                    status="PAUSED",
                    total=len(all_days),
                    processed=sum(1 for item in all_days if item < day),
                    message=str(exc)[:500],
                    payload=payload,
                )
                raise

        if paused_at:
            state_cursor = paused_at
            next_cursor = paused_at
            done = False
            processed = sum(1 for day in all_days if day < paused_at)
        else:
            more = len(days) > len(batch)
            last_day = batch[-1]
            state_cursor = days[len(batch)] if more else (
                datetime.strptime(last_day, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            next_cursor = days[len(batch)] if more else None
            done = not more and not pending
            processed = sum(1 for day in all_days if day < state_cursor)

        self.state.save(
            TASK_NAME,
            state_dataset,
            cursor=state_cursor,
            status="DONE" if done else ("PAUSED" if paused_at else "RUNNING"),
            total=len(all_days),
            processed=processed,
            message="; ".join(errors[:5]) if errors else None,
            payload=payload,
        )
        return {
            "dataset": "daily",
            "codes": selected_codes,
            "include_funds": include_funds,
            "done": done,
            "total": len(all_days),
            "processed": processed,
            "next_cursor": next_cursor,
            "rows_written": rows_written,
            "fund_rows_written": fund_rows_written,
            "pending_days": sorted(pending)[:10],
            "errors": errors[:10],
        }

    def _handle_empty_day(
        self,
        day: str,
        attempts: dict[str, int],
        pending: list[str],
        errors: list[str],
        retry_limit: int,
    ) -> bool:
        """某交易日无数据：返回 True 表示应暂停在该日等待下次重试。"""
        attempts[day] = int(attempts.get(day, 0)) + 1
        if day not in pending:
            pending.append(day)
        if day >= today_str() and not market_closed_today():
            # 当天尚未收盘，Tushare 日线还没发布：不算错误，明天再补
            logger.info("%s 尚未发布日线（未收盘），留待下次心跳", day)
            return False
        if attempts[day] >= retry_limit:
            errors.append(f"{day}: 连续 {attempts[day]} 次无数据，跳过")
            logger.warning("%s 连续 %s 次无数据，跳过", day, attempts[day])
            if day in pending:
                pending.remove(day)
            return False
        logger.warning("%s 无数据（第 %s 次），暂停后重试", day, attempts[day])
        return True

    def _attach_factors(self, frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """贴上该交易日的全市场复权因子（拿不到就写 NULL，读取端会标记降级）。"""
        if frame.empty:
            return frame
        try:
            factors = self.adj_factors.factors_for_trade_date(trade_date)
        except Exception as exc:
            logger.warning("复权因子获取失败 %s: %s", trade_date, exc)
            factors = {}
        if factors:
            frame["adj_factor"] = frame["code"].map(factors)
        else:
            frame["adj_factor"] = pd.NA
        return frame

    @staticmethod
    def _daily_frame(
        raw_rows: list[dict[str, Any]],
        basic_rows: list[dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        frame = pd.DataFrame(raw_rows)
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        for source in ("open", "high", "low", "close"):
            frame[source] = pd.to_numeric(frame.get(source), errors="coerce")
        # Tushare daily: vol 单位「手」-> 股，amount 单位「千元」-> 元
        frame["volume"] = pd.to_numeric(frame.get("vol"), errors="coerce").fillna(0) * 100
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0) * 1000

        # 换手率来自 daily_basic.turnover_rate（%），避免把已有换手率覆盖成 0
        turnover = {}
        for item in basic_rows or []:
            code = normalize_code(str(item.get("ts_code") or ""))
            value = item.get("turnover_rate")
            if code and value is not None:
                try:
                    turnover[code] = float(value)
                except (TypeError, ValueError):
                    continue
        frame["turnover"] = frame["code"].map(turnover).astype("float64")
        frame = frame.dropna(subset=["ts", "code", "open", "high", "low", "close"])
        frame.set_index("ts", inplace=True)
        return frame[["code", "open", "high", "low", "close", "volume", "amount", "turnover"]]

    @staticmethod
    def _fund_daily_frame(raw_rows: list[dict[str, Any]]) -> pd.DataFrame:
        frame = pd.DataFrame(raw_rows or [])
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        for source in ("open", "high", "low", "close"):
            frame[source] = pd.to_numeric(frame.get(source), errors="coerce")
        # Tushare fund_daily: vol 单位「手」-> 股，amount 单位「千元」-> 元
        frame["volume"] = pd.to_numeric(frame.get("vol"), errors="coerce").fillna(0) * 100
        frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0) * 1000
        frame["turnover"] = pd.NA
        frame = frame.dropna(subset=["ts", "code", "open", "high", "low", "close"])
        frame.set_index("ts", inplace=True)
        return frame[["code", "open", "high", "low", "close", "volume", "amount", "turnover"]]

    def _ensure_fund_universe(self) -> int:
        """确保场内基金基础信息已落库（ETF/LOF 查询与同步都依赖它）。"""
        min_count = int(self.settings.get("fund_universe_min_count", 500))
        existing = self.financial.repository.get_fund_codes()
        if len(existing) >= min_count:
            return len(existing)

        raw = self.tushare.fetch_fund_basic_all("E")
        if not raw:
            return len(existing)

        snapshot = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        for item in raw:
            code = normalize_code(str(item.get("ts_code") or ""))
            if not code or self.market_fetcher.get_ticker_type(code) not in ("etf", "lof"):
                continue
            rows.append(
                {
                    "ts": snapshot,
                    "code": code,
                    "name": item.get("name"),
                    "fullname": item.get("name"),
                    "found_date": pd.to_datetime(item.get("found_date"), errors="coerce"),
                    "total_asset": None,
                    "management": item.get("management"),
                    "fund_type": item.get("fund_type"),
                    "rating_agency": None,
                    "rating": None,
                }
            )
        if not rows:
            return len(existing)
        frame = pd.DataFrame(rows)
        written = self.market_fetcher.update_fund_basic(frame)
        logger.info("基金基础信息同步 %s 条", written)
        return len(existing) + written

    # ------------------------------------------------------------------ 财务
