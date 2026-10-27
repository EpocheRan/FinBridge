"""PyBroker 引擎适配（MVP：单股）。"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pybroker
from pybroker import Strategy, StrategyConfig
from pybroker.common import FeeInfo, PositionMode, PriceType

from backtest.application.cross_section import should_keep_buffer
from backtest.application.ports import EngineRequest
from backtest.config import BacktestSettings
from backtest.domain.errors import EngineError
from backtest.domain.models import BacktestResult, ExecutionReport
from backtest.infrastructure.execution.a_share import (
    FeeModel,
    TPlusOneLedger,
    can_buy,
    can_sell,
    round_lot,
)
from backtest.infrastructure.execution.ledger import PortfolioLedger, SimpleLedger, reconcile
from strategies.context import StrategyContext


def _status_flags(status, symbol, dt, calendar, rules):
    if status is None:
        return False, False
    return (
        status.is_st(symbol, dt),
        status.is_new_listing(
            symbol, dt, calendar, rules.new_listing_no_limit_days
        ),
    )


def _can_buy_with_status(*, row, prev_close, code, rules, status, calendar, dt):
    is_st, new_listing = _status_flags(status, code, dt, calendar, rules)
    return can_buy(
        row=row,
        prev_close=prev_close,
        code=code,
        rules=rules,
        is_st=is_st,
        new_listing=new_listing,
    )


def _can_sell_with_status(*, row, prev_close, code, rules, status, calendar, dt):
    is_st, new_listing = _status_flags(status, code, dt, calendar, rules)
    return can_sell(
        row=row,
        prev_close=prev_close,
        code=code,
        rules=rules,
        is_st=is_st,
        new_listing=new_listing,
    )


def _metrics_pybroker(result) -> dict[str, float]:
    frame = getattr(result, "metrics_df", None)
    if frame is None or frame.empty:
        return {}
    if {"name", "value"}.issubset(frame.columns):
        metrics: dict[str, float] = {}
        for _, row in frame.iterrows():
            value = row["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if pd.notna(value):
                metrics[str(row["name"])] = float(value)
        return metrics
    return {}


def _make_fee_fn(rule_engine, fee_model, current_dt):
    """佣金/过户费/印花税（卖出）统一入口，单股与横截面共用。"""

    def fee_fn(info: FeeInfo) -> Decimal:
        value = info.fill_price * info.shares
        commission = max(
            value * Decimal(str(rule_engine.commission_rate)),
            Decimal(str(rule_engine.min_commission)),
        )
        transfer = value * Decimal(str(rule_engine.transfer_fee_rate))
        stamp = (
            value * Decimal(str(fee_model.stamp_rate(current_dt[0])))
            if str(info.order_type).lower() == "sell"
            else Decimal("0")
        )
        return commission + transfer + stamp

    return fee_fn


class PyBrokerEngine:
    name = "pybroker"

    def run(self, request: EngineRequest) -> BacktestResult:
        if request.strategy.mode == "single":
            return self._run_single(request)
        if request.strategy.mode == "cross":
            return self._run_cross(request)
        raise EngineError(f"未知策略模式: {request.strategy.mode!r}")

    # ------------------------------------------------------------------ 单股
    def _run_single(self, request: EngineRequest) -> BacktestResult:
        spec = request.spec
        symbols = tuple(spec.symbols)
        if len(symbols) != 1:
            raise EngineError("单股策略必须且只能声明一个 symbol")
        symbol = symbols[0]

        data = request.data.frame.copy()
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
        data = data[data["symbol"] == symbol].sort_values("date").reset_index(drop=True)
        if data.empty:
            raise EngineError(f"没有 {symbol} 的数据")

        data["prev_close"] = data["close"].shift(1)
        by_key = {
            (str(row["symbol"]), pd.Timestamp(row["date"]).normalize()): row
            for _, row in data.iterrows()
        }
        prev_close_map = {
            (str(row["symbol"]), pd.Timestamp(row["date"]).normalize()): (
                None if pd.isna(row["prev_close"]) else float(row["prev_close"])
            )
            for _, row in data.iterrows()
        }

        rule_engine = request.rules
        security_status = request.data.security_status
        fee_model = FeeModel(rule_engine)
        t1 = TPlusOneLedger(enabled=rule_engine.t_plus_1)
        report = ExecutionReport()
        current_dt: list[pd.Timestamp | None] = [None]
        fee_fn = _make_fee_fn(rule_engine, fee_model, current_dt)

        config = StrategyConfig(
            initial_cash=float(spec.initial_cash),
            fee_mode=fee_fn,
            position_mode=PositionMode.LONG_ONLY,
            buy_delay=1,
            sell_delay=1,
            exit_on_last_bar=False,
        )
        strategy = Strategy(
            data[["symbol", "date", "open", "high", "low", "close", "volume"]],
            str(spec.start),
            str(spec.end),
            config=config,
        )

        fill_type = PriceType.OPEN if spec.fill == "next_open" else PriceType.CLOSE
        gross_exposure = float(spec.max_gross_exposure)
        processed_orders: set[int] = set()

        def _sync_fills(py_ctx) -> None:
            """按**成交**更新 T+1 台账：未成交/被裁剪的订单不会记成已买入。"""
            for order in py_ctx.orders():
                order_id = int(order.id)
                if order_id in processed_orders:
                    continue
                processed_orders.add(order_id)
                shares = float(order.shares)
                if shares <= 0:
                    continue
                if str(order.type).lower() == "buy":
                    t1.on_buy(symbol, shares)
                else:
                    t1.on_sell(symbol, shares)

        def apply_intent(py_ctx, sctx: StrategyContext, row) -> None:
            intent = sctx.intent
            price = float(row["close"])
            if not (price > 0):
                report.reject(dt=sctx.dt, symbol=symbol, reason="price_invalid")
                return
            pos = int(py_ctx.long_pos().shares) if py_ctx.long_pos() else 0
            # 卖单在下一根 bar 成交，所以“今日成交的买入”也可以挂卖单（T+1 允许次日卖）。
            sellable = t1.sellable(symbol)
            if sellable == float("inf"):
                sellable = float(pos)

            explicit_sell = (
                intent.close_position
                or intent.sell_shares is not None
                or intent.sell_percent is not None
            )
            if explicit_sell:
                if intent.close_position:
                    requested = pos
                elif intent.sell_shares is not None:
                    requested = int(intent.sell_shares)
                else:
                    requested = int(pos * float(intent.sell_percent or 0))
                requested = min(requested, pos, int(sellable))
                if requested <= 0:
                    reason = "t_plus_1" if pos > 0 and sellable <= 0 else "no_position"
                    report.reject(
                        dt=sctx.dt,
                        symbol=symbol,
                        reason=reason,
                        requested=intent.sell_shares or 0,
                    )
                    return
                allowed, reason = _can_sell_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=sctx.dt,
                )
                if not allowed:
                    report.reject(
                        dt=sctx.dt, symbol=symbol, reason=reason, requested=requested
                    )
                    return
                py_ctx.sell_shares = requested
                py_ctx.sell_fill_price = fill_type
                return

            if intent.target_weight is not None:
                weight = min(float(intent.target_weight), gross_exposure)
                target = round_lot(
                    weight * float(py_ctx.total_equity) / price,
                    rule_engine.lot_size,
                )
                if target > pos:
                    requested = target - pos
                    side = "buy"
                elif target < pos:
                    requested = min(pos - target, int(sellable))
                    side = "sell"
                    if requested <= 0:
                        report.reject(
                            dt=sctx.dt,
                            symbol=symbol,
                            reason="t_plus_1",
                            requested=pos - target,
                        )
                        return
                else:
                    return
            elif intent.buy_shares is not None:
                requested = int(intent.buy_shares)
                side = "buy"
                # 单股也要受 max_gross_exposure 约束：按目标市值上限裁剪增量。
                max_shares = round_lot(
                    gross_exposure * float(py_ctx.total_equity) / price,
                    rule_engine.lot_size,
                )
                room = max(0, max_shares - pos)
                if requested > room:
                    report.unfill(
                        dt=sctx.dt,
                        symbol=symbol,
                        reason="exposure",
                        target=requested,
                        actual=room,
                    )
                    requested = room
                    if requested <= 0:
                        report.reject(
                            dt=sctx.dt, symbol=symbol, reason="exposure", requested=0
                        )
                        return
            else:
                if any(
                    value is not None
                    for value in (
                        intent.stop_loss,
                        intent.stop_loss_pct,
                        intent.trailing_pct,
                        intent.hold_bars,
                    )
                ):
                    report.reject(dt=sctx.dt, symbol=symbol, reason="stop_requires_order")
                return

            if side == "buy":
                requested = round_lot(requested, rule_engine.lot_size)
                if requested <= 0:
                    report.reject(
                        dt=sctx.dt, symbol=symbol, reason="lot_size", requested=requested
                    )
                    return
                allowed, reason = can_buy(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                    is_st=(
                        security_status.is_st(symbol, sctx.dt)
                        if security_status is not None
                        else False
                    ),
                    new_listing=(
                        security_status.is_new_listing(
                            symbol,
                            sctx.dt,
                            request.calendar,
                            rule_engine.new_listing_no_limit_days,
                        )
                        if security_status is not None
                        else False
                    ),
                )
                if not allowed:
                    report.reject(
                        dt=sctx.dt, symbol=symbol, reason=reason, requested=requested
                    )
                    return
                max_cash = int(float(py_ctx.buying_power) / (price * 1.001))
                max_cash = round_lot(max_cash, rule_engine.lot_size)
                if requested > max_cash:
                    report.unfill(
                        dt=sctx.dt,
                        symbol=symbol,
                        reason="cash",
                        target=requested,
                        actual=max_cash,
                    )
                    requested = max_cash
                if requested <= 0:
                    report.reject(dt=sctx.dt, symbol=symbol, reason="cash", requested=0)
                    return
                py_ctx.buy_shares = requested
                py_ctx.buy_fill_price = fill_type
                if pos == 0:
                    if intent.stop_loss is not None:
                        py_ctx.stop_loss = float(intent.stop_loss)
                    if intent.stop_loss_pct is not None:
                        py_ctx.stop_loss_pct = float(intent.stop_loss_pct)
                    if intent.trailing_pct is not None:
                        py_ctx.stop_trailing = float(intent.trailing_pct)
                    if intent.hold_bars is not None:
                        py_ctx.hold_bars = int(intent.hold_bars)
            else:
                if requested <= 0:
                    return
                allowed, reason = _can_sell_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=sctx.dt,
                )
                if not allowed:
                    report.reject(
                        dt=sctx.dt, symbol=symbol, reason=reason, requested=requested
                    )
                    return
                py_ctx.sell_shares = requested
                py_ctx.sell_fill_price = fill_type

        def exec_fn(py_ctx) -> None:
            dt = pd.Timestamp(py_ctx.dt).normalize()
            current_dt[0] = dt
            row = by_key.get((symbol, dt))
            if row is None:
                return
            t1.start_day(dt.date())
            _sync_fills(py_ctx)
            pos = int(py_ctx.long_pos().shares) if py_ctx.long_pos() else 0
            equity = float(py_ctx.total_equity)
            close = float(row["close"])
            weight = (pos * close / equity) if equity > 0 else 0.0
            bars_held = int(getattr(py_ctx.long_pos(), "bars", 0)) if py_ctx.long_pos() else 0
            sctx = StrategyContext(
                symbol=symbol,
                dt=dt,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=close,
                hfq_close=float(row.get("hfq_close", close)),
                pos=float(pos),
                cash=float(py_ctx.cash),
                equity=equity,
                weight=weight,
                bars_held=bars_held,
                panel=request.panel.view(dt),
                rank=None,
                selected=True,
                universe_size=1,
                is_rebalance=True,
            )
            request.strategy.on_bar(sctx)
            apply_intent(py_ctx, sctx, row)

        strategy.add_execution(exec_fn, [symbol])
        try:
            result = strategy.backtest(seed=spec.seed)
        except ValueError as exc:
            raise EngineError(f"PyBroker 回测失败: {exc}") from exc

        ledger_data = data[data["date"] >= pd.Timestamp(spec.start).normalize()].copy()
        ledger_result = SimpleLedger(
            initial_cash=float(spec.initial_cash),
            data=ledger_data,
            orders=result.orders,
            symbol=symbol,
            benchmark=request.benchmark,
            risk_free_rate=spec.risk_free_rate,
            corporate_actions=request.corporate_actions,
        ).run()
        recon = reconcile(ledger_result.equity, getattr(result, "portfolio", None))

        selections = pd.DataFrame(columns=["date", "symbol", "score", "rank", "weight"])
        open_positions_at_end = bool(
            not ledger_result.equity.empty
            and float(ledger_result.equity.iloc[-1]["market_value"]) > 0
        )
        meta = {
            "engine": "pybroker",
            "pybroker_version": pybroker.__version__,
            "fill": spec.fill,
            "fill_price_type": fill_type.value,
            "metrics_source": "ledger",
            "corporate_actions": spec.corporate_actions,
            "corporate_action_approximation": (
                spec.corporate_actions == "approx"
            ),
            "allow_short": spec.allow_short,
            "max_gross_exposure": spec.max_gross_exposure,
            "benchmark": spec.benchmark,
            "benchmark_used": bool(
                request.benchmark is not None and "beta" in ledger_result.metrics
            ),
            "benchmark_points": int(ledger_result.metrics.get("benchmark_points", 0)),
            "risk_free_rate": spec.risk_free_rate,
            "st_approximation": (
                True
                if security_status is None
                else bool(security_status.approximation)
            ),
            "new_listing_no_limit_days": rule_engine.new_listing_no_limit_days,
            "seed": spec.seed,
            "open_positions_at_end": open_positions_at_end,
            "ledger_reconciliation": recon,
            "orders_filled": int(len(result.orders)),
            "trades": int(len(result.trades)),
        }
        return BacktestResult(
            run_id="",
            spec=spec,
            metrics=ledger_result.metrics,
            metrics_pybroker=_metrics_pybroker(result),
            equity=ledger_result.equity,
            orders=result.orders.reset_index(),
            trades=result.trades.reset_index(),
            selections=selections,
            factor_meta=dict(request.panel.meta),
            execution_report=report,
            corporate_actions=(
                None
                if request.corporate_actions is None
                else request.corporate_actions.copy()
            ),
            meta=meta,
        )

    # ------------------------------------------------------------------ 横截面
    def _run_cross(self, request: EngineRequest) -> BacktestResult:
        plan = request.cross_plan
        if plan is None:
            raise EngineError("横截面引擎缺少 cross_plan")
        spec = request.spec
        candidates = tuple(plan.candidates)
        if not candidates:
            raise EngineError("横截面候选集为空")

        data = request.data.frame.copy()
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
        data = (
            data[data["symbol"].isin(candidates)]
            .dropna(subset=["date"])
            .sort_values(["symbol", "date"])
            .reset_index(drop=True)
        )
        if data.empty:
            raise EngineError("候选集没有行情数据")
        data["prev_close"] = data.groupby("symbol")["close"].shift(1)
        by_key = {
            (str(row["symbol"]), pd.Timestamp(row["date"]).normalize()): row
            for _, row in data.iterrows()
        }
        prev_close_map = {
            (str(row["symbol"]), pd.Timestamp(row["date"]).normalize()): (
                None if pd.isna(row["prev_close"]) else float(row["prev_close"])
            )
            for _, row in data.iterrows()
        }

        rule_engine = request.rules
        security_status = request.data.security_status
        fee_model = FeeModel(rule_engine)
        t1 = TPlusOneLedger(enabled=rule_engine.t_plus_1)
        report = ExecutionReport()
        current_dt: list[pd.Timestamp | None] = [None]
        fee_fn = _make_fee_fn(rule_engine, fee_model, current_dt)
        config = StrategyConfig(
            initial_cash=float(spec.initial_cash),
            fee_mode=fee_fn,
            position_mode=PositionMode.LONG_ONLY,
            buy_delay=1,
            sell_delay=1,
            exit_on_last_bar=False,
        )
        strategy = Strategy(
            data[["symbol", "date", "open", "high", "low", "close", "volume"]],
            str(spec.start),
            str(spec.end),
            config=config,
        )
        fill_type = PriceType.OPEN if spec.fill == "next_open" else PriceType.CLOSE
        gross_exposure = float(spec.max_gross_exposure)
        lot = int(rule_engine.lot_size)
        processed_orders: set[int] = set()
        positions: dict[str, int] = {}
        view_cache: dict[pd.Timestamp, Any] = {}
        rank_cache: dict[pd.Timestamp, pd.Series] = {}
        last_sync_day: list[pd.Timestamp | None] = [None]
        pending_target: dict[str, float] | None = None
        pending_symbols: set[str] = set()
        blocked: set[str] = set()

        def row_for(symbol: str, dt: pd.Timestamp):
            return by_key.get((symbol, dt))

        def view_for(dt: pd.Timestamp):
            view = view_cache.get(dt)
            if view is None:
                view = request.panel.view(dt)
                view_cache[dt] = view
            return view

        def ranks_for(dt: pd.Timestamp) -> pd.Series:
            series = rank_cache.get(dt)
            if series is None:
                series = (
                    plan.ranks.loc[dt]
                    if dt in plan.ranks.index
                    else pd.Series(dtype="float64")
                )
                rank_cache[dt] = series
            return series

        def rank_for(dt: pd.Timestamp, symbol: str) -> float | None:
            series = ranks_for(dt)
            if symbol not in series.index:
                return None
            value = series.get(symbol)
            return None if pd.isna(value) else float(value)

        def sync_for_bar(py_ctx, dt: pd.Timestamp) -> None:
            """每根 bar 只同步一次成交流水（order 列表是全组合的）。"""
            if last_sync_day[0] == dt:
                return
            last_sync_day[0] = dt
            # 新的调仓日重置“风险退出后不再回补”的屏蔽集；同一 bar 内
            # 随后发生的风险退出仍会加入 blocked，不会被本 bar 回补。
            if plan.target_for(dt) is not None:
                blocked.clear()
            t1.start_day(dt.date())
            sync_fills(py_ctx)

        def sync_fills(py_ctx) -> None:
            for order in py_ctx.orders():
                order_id = int(order.id)
                if order_id in processed_orders:
                    continue
                processed_orders.add(order_id)
                shares = float(order.shares)
                if shares <= 0:
                    continue
                symbol = str(order.symbol)
                if str(order.type).lower() == "buy":
                    t1.on_buy(symbol, shares)
                    positions[symbol] = positions.get(symbol, 0) + int(shares)
                else:
                    t1.on_sell(symbol, shares)
                    positions[symbol] = max(0, positions.get(symbol, 0) - int(shares))

        def context(py_ctx, dt: pd.Timestamp) -> StrategyContext:
            symbol = str(py_ctx.symbol)
            row = row_for(symbol, dt)
            position = py_ctx.long_pos()
            pos = int(position.shares) if position else 0
            equity = float(py_ctx.total_equity)
            close = float(row["close"])
            target = plan.target_for(dt)
            rank = rank_for(dt, symbol)
            selected = (
                symbol in target
                if target is not None
                else (rank is not None and rank <= plan.top_n)
            )
            return StrategyContext(
                symbol=symbol,
                dt=dt,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=close,
                hfq_close=float(row.get("hfq_close", close)),
                pos=float(pos),
                cash=float(py_ctx.cash),
                equity=equity,
                weight=(pos * close / equity) if equity > 0 else 0.0,
                bars_held=int(getattr(position, "bars", 0)) if position else 0,
                panel=view_for(dt),
                rank=rank,
                selected=selected,
                universe_size=plan.universe_size,
                is_rebalance=target is not None,
            )

        def sellable_for(symbol: str, pos: int) -> int:
            value = t1.sellable(symbol)
            return pos if value == float("inf") else min(pos, int(value))

        def apply_risk(py_ctx, sctx: StrategyContext) -> None:
            intent = sctx.intent
            symbol = sctx.symbol
            row = row_for(symbol, sctx.dt)
            pos = int(py_ctx.long_pos().shares) if py_ctx.long_pos() else 0
            if (
                intent.close_position
                or intent.sell_shares is not None
                or intent.sell_percent is not None
            ):
                if intent.close_position:
                    requested = pos
                elif intent.sell_shares is not None:
                    requested = int(intent.sell_shares)
                else:
                    requested = int(pos * float(intent.sell_percent or 0))
                requested = min(requested, sellable_for(symbol, pos))
                if requested <= 0:
                    report.reject(
                        dt=sctx.dt,
                        symbol=symbol,
                        reason="t_plus_1" if pos > 0 else "no_position",
                        requested=intent.sell_shares or 0,
                    )
                    return
                allowed, reason = _can_sell_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=sctx.dt,
                )
                if not allowed:
                    report.reject(
                        dt=sctx.dt, symbol=symbol, reason=reason, requested=requested
                    )
                    return
                py_ctx.sell_shares = requested
                py_ctx.sell_fill_price = fill_type
                blocked.add(symbol)
                return
            if intent.target_weight is not None or intent.buy_shares is not None:
                # 横截面开仓统一在 after_exec 的调仓层完成，避免 risk_fn 与目标权重打架。
                report.reject(
                    dt=sctx.dt, symbol=symbol, reason="cross_entry_in_risk", requested=0
                )
                return
            if any(
                value is not None
                for value in (
                    intent.stop_loss,
                    intent.stop_loss_pct,
                    intent.trailing_pct,
                    intent.hold_bars,
                )
            ):
                report.reject(dt=sctx.dt, symbol=symbol, reason="stop_requires_order")

        def risk_fn(py_ctx) -> None:
            dt = pd.Timestamp(py_ctx.dt).normalize()
            current_dt[0] = dt
            symbol = str(py_ctx.symbol)
            if row_for(symbol, dt) is None:
                return
            sync_for_bar(py_ctx, dt)
            position = py_ctx.long_pos()
            if position is None or int(position.shares) <= 0:
                # 未持仓标的的开仓统一由 after_exec 完成；这里跳过可省掉
                # 每 bar 每标的的 context 构造（全 A 场景下是主要开销）。
                return
            sctx = context(py_ctx, dt)
            request.strategy.on_bar(sctx)
            apply_risk(py_ctx, sctx)

        def desired_shares(target, symbol: str, equity: float, dt: pd.Timestamp) -> int:
            weight = min(float(target.get(symbol, 0.0)), gross_exposure)
            if weight <= 0:
                return 0
            row = row_for(symbol, dt)
            price = float(row["close"]) if row is not None else 0.0
            if price <= 0:
                return 0
            return round_lot(weight * equity / price, lot)

        def apply_targets(
            ctxs,
            dt: pd.Timestamp,
            target: Mapping[str, float],
            *,
            only: set[str] | None = None,
        ) -> set[str]:
            """执行目标权重，返回未完成/被拒绝、需要在下一 bar 重试的 symbol。"""
            target = {
                symbol: float(weight)
                for symbol, weight in target.items()
                if np.isfinite(weight) and float(weight) > 0
            }
            only_symbols = set(only) if only is not None else None
            equity = float(next(iter(ctxs.values())).total_equity)
            cash = float(next(iter(ctxs.values())).cash)
            held = [
                symbol
                for symbol, qty in positions.items()
                if qty > 0 and symbol in ctxs
            ]
            retained = {
                symbol
                for symbol in held
                if symbol not in target
                and should_keep_buffer(rank_for(dt, symbol), plan.top_n, plan.buffer)
            }

            # buffer 保留的持仓也占用 gross exposure，target 只能使用剩余额度。
            retained_weight = 0.0
            if equity > 0:
                for symbol in retained:
                    row = row_for(symbol, dt)
                    position = ctxs[symbol].long_pos()
                    if row is not None and position is not None:
                        retained_weight += (
                            int(position.shares) * float(row["close"]) / equity
                        )
            target_total = sum(target.values())
            if target_total > 0 and retained_weight + target_total > gross_exposure:
                room = max(0.0, gross_exposure - retained_weight)
                scale = room / target_total
                target = {symbol: weight * scale for symbol, weight in target.items()}

            desired: dict[str, int] = {}
            for symbol in set(held) | set(target):
                if symbol not in ctxs:
                    continue
                if symbol in retained:
                    position = ctxs[symbol].long_pos()
                    desired[symbol] = int(position.shares) if position else 0
                else:
                    desired[symbol] = desired_shares(target, symbol, equity, dt)

            pending: set[str] = set()
            proceeds = 0.0

            # 先卖：卖出回款在 PyBroker 中先于买单结算（已验证）。
            for symbol in held:
                if only_symbols is not None and symbol not in only_symbols:
                    continue
                if symbol in retained or symbol in blocked:
                    continue
                ctx = ctxs[symbol]
                row = row_for(symbol, dt)
                if row is None:
                    pending.add(symbol)
                    continue
                if ctx.sell_shares is not None:
                    continue
                pos = int(ctx.long_pos().shares) if ctx.long_pos() else 0
                want = desired.get(symbol, 0)
                if pos <= want:
                    continue
                delta = pos - want
                requested = sellable_for(symbol, delta)
                if requested <= 0:
                    report.reject(
                        dt=dt, symbol=symbol, reason="t_plus_1", requested=delta
                    )
                    pending.add(symbol)
                    continue
                allowed, reason = _can_sell_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=dt,
                )
                if not allowed:
                    report.reject(
                        dt=dt, symbol=symbol, reason=reason, requested=requested
                    )
                    pending.add(symbol)
                    continue
                ctx.sell_shares = requested
                ctx.sell_fill_price = fill_type
                proceeds += requested * float(row["close"]) * 0.998
                if requested < delta:
                    pending.add(symbol)

            # 留出费用/跳空缓冲：卖出回款按 95% 计，且至少保留 1000 元现金。
            reserve = max(1_000.0, 0.002 * equity)
            budget = max(0.0, cash + proceeds * 0.95 - reserve)

            # 再买：按目标权重从大到小分配剩余现金。
            for symbol in sorted(
                target, key=lambda item: float(target.get(item, 0.0)), reverse=True
            ):
                if only_symbols is not None and symbol not in only_symbols:
                    continue
                if symbol in blocked:
                    continue
                ctx = ctxs.get(symbol)
                row = row_for(symbol, dt)
                if ctx is None or row is None:
                    pending.add(symbol)
                    continue
                if ctx.sell_shares is not None or ctx.buy_shares is not None:
                    continue
                pos = int(ctx.long_pos().shares) if ctx.long_pos() else 0
                want = desired.get(symbol, 0)
                if want <= pos:
                    continue
                delta = want - pos
                allowed, reason = _can_buy_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=dt,
                )
                if not allowed:
                    report.reject(
                        dt=dt, symbol=symbol, reason=reason, requested=delta
                    )
                    pending.add(symbol)
                    continue
                price = float(row["close"])
                affordable = round_lot(max(0.0, budget) / (price * 1.002), lot)
                requested = min(delta, affordable)
                if requested <= 0:
                    report.unfill(
                        dt=dt,
                        symbol=symbol,
                        reason="cash",
                        target=delta,
                        actual=0,
                    )
                    pending.add(symbol)
                    continue
                ctx.buy_shares = requested
                ctx.buy_fill_price = fill_type
                budget -= requested * price * 1.002
                if requested < delta:
                    pending.add(symbol)

            return pending

        def apply_buffer_exits(ctxs, dt: pd.Timestamp) -> set[str]:
            threshold = plan.top_n + plan.buffer
            sold: set[str] = set()
            for symbol, qty in positions.items():
                if qty <= 0:
                    continue
                ctx = ctxs.get(symbol)
                if ctx is None or ctx.sell_shares is not None:
                    continue
                pos = int(ctx.long_pos().shares) if ctx.long_pos() else 0
                if pos <= 0:
                    continue
                rank = rank_for(dt, symbol)
                if rank is None or rank <= threshold:
                    continue
                row = row_for(symbol, dt)
                requested = sellable_for(symbol, pos)
                if requested <= 0:
                    report.reject(dt=dt, symbol=symbol, reason="t_plus_1", requested=pos)
                    continue
                allowed, reason = _can_sell_with_status(
                    row=row,
                    prev_close=prev_close_map.get((symbol, dt)),
                    code=symbol,
                    rules=rule_engine,
                    status=security_status,
                    calendar=request.calendar,
                    dt=dt,
                )
                if not allowed:
                    report.reject(
                        dt=dt, symbol=symbol, reason=reason, requested=requested
                    )
                    continue
                ctx.sell_shares = requested
                ctx.sell_fill_price = fill_type
                blocked.add(symbol)
                sold.add(symbol)
            return sold

        def rebalance_fn(ctxs) -> None:
            nonlocal pending_target, pending_symbols
            if not ctxs:
                return
            dt = pd.Timestamp(next(iter(ctxs.values())).dt).normalize()
            current_dt[0] = dt
            target = plan.target_for(dt)
            if target is not None:
                pending_target = dict(target)
                pending_symbols = apply_targets(ctxs, dt, pending_target)
                return

            sold = apply_buffer_exits(ctxs, dt)
            if pending_target is not None:
                for symbol in sold:
                    pending_target.pop(symbol, None)
                    pending_symbols.discard(symbol)
                if pending_symbols:
                    pending_symbols = apply_targets(
                        ctxs, dt, pending_target, only=pending_symbols
                    )

        strategy.add_execution(risk_fn, list(candidates))
        strategy.set_after_exec(rebalance_fn)
        try:
            result = strategy.backtest(seed=spec.seed)
        except ValueError as exc:
            raise EngineError(f"PyBroker 回测失败: {exc}") from exc

        ledger_data = data[data["date"] >= pd.Timestamp(spec.start).normalize()].copy()
        ledger_result = PortfolioLedger(
            initial_cash=float(spec.initial_cash),
            data=ledger_data,
            orders=result.orders,
            symbols=candidates,
            benchmark=request.benchmark,
            risk_free_rate=spec.risk_free_rate,
            corporate_actions=request.corporate_actions,
        ).run()
        recon = reconcile(
            ledger_result.equity, getattr(result, "portfolio", None), tolerance=1e-6
        )
        open_positions_at_end = bool(
            not ledger_result.equity.empty
            and float(ledger_result.equity.iloc[-1]["market_value"]) > 0
        )
        meta = {
            "engine": "pybroker",
            "pybroker_version": pybroker.__version__,
            "mode": "cross",
            "fill": spec.fill,
            "fill_price_type": fill_type.value,
            "metrics_source": "ledger",
            "corporate_actions": spec.corporate_actions,
            "corporate_action_approximation": (
                spec.corporate_actions == "approx"
            ),
            "allow_short": spec.allow_short,
            "max_gross_exposure": spec.max_gross_exposure,
            "benchmark": spec.benchmark,
            "benchmark_used": bool(
                request.benchmark is not None and "beta" in ledger_result.metrics
            ),
            "benchmark_points": int(ledger_result.metrics.get("benchmark_points", 0)),
            "risk_free_rate": spec.risk_free_rate,
            "st_approximation": (
                True
                if security_status is None
                else bool(security_status.approximation)
            ),
            "new_listing_no_limit_days": rule_engine.new_listing_no_limit_days,
            "seed": spec.seed,
            "open_positions_at_end": open_positions_at_end,
            "universe_size": plan.universe_size,
            "candidate_count": len(candidates),
            "rebalance_days": len(plan.rebalance_dates),
            "top_n": plan.top_n,
            "buffer": plan.buffer,
            "stops": "strategy_managed",
            "ledger_reconciliation": recon,
            "orders_filled": int(len(result.orders)),
            "trades": int(len(result.trades)),
        }
        return BacktestResult(
            run_id="",
            spec=spec,
            metrics=ledger_result.metrics,
            metrics_pybroker=_metrics_pybroker(result),
            equity=ledger_result.equity,
            orders=result.orders.reset_index(),
            trades=result.trades.reset_index(),
            selections=plan.selections.copy(),
            factor_meta=dict(request.panel.meta),
            execution_report=report,
            corporate_actions=(
                None
                if request.corporate_actions is None
                else request.corporate_actions.copy()
            ),
            meta=meta,
        )
