"""PyBroker 引擎适配（MVP：单股）。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pybroker
from pybroker import Strategy, StrategyConfig
from pybroker.common import FeeInfo, PositionMode, PriceType

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
from backtest.infrastructure.execution.ledger import SimpleLedger, reconcile
from strategies.context import StrategyContext


def _metrics_pybroker(result) -> dict[str, float]:
    frame = getattr(result, "metrics_df", None)
    if frame is None or frame.empty:
        return {}
    if {"name", "value"}.issubset(frame.columns):
        result: dict[str, float] = {}
        for _, row in frame.iterrows():
            value = row["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if pd.notna(value):
                result[str(row["name"])] = float(value)
        return result
    return {}


class PyBrokerEngine:
    name = "pybroker"

    def run(self, request: EngineRequest) -> BacktestResult:
        if request.strategy.mode != "single":
            raise EngineError(
                "MVP 只实现单股引擎；横截面引擎在下一步接入 "
                "add_execution(risk_fn) + set_after_exec"
            )
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
        fee_model = FeeModel(rule_engine)
        t1 = TPlusOneLedger(enabled=rule_engine.t_plus_1)
        report = ExecutionReport()
        current_dt: list[pd.Timestamp | None] = [None]

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

        def apply_intent(py_ctx, sctx: StrategyContext, row) -> None:
            intent = sctx.intent
            price = float(row["close"])
            if not (price > 0):
                report.reject(dt=sctx.dt, symbol=symbol, reason="price_invalid")
                return
            pos = int(py_ctx.long_pos().shares) if py_ctx.long_pos() else 0
            available = t1.available(symbol)
            if available == float("inf"):
                available = float(pos)

            explicit_sell = intent.close_position or intent.sell_shares is not None or intent.sell_percent is not None
            if explicit_sell:
                if intent.close_position:
                    requested = pos
                elif intent.sell_shares is not None:
                    requested = int(intent.sell_shares)
                else:
                    requested = int(pos * float(intent.sell_percent or 0))
                requested = min(requested, pos, int(available))
                if requested <= 0:
                    reason = "t_plus_1" if pos > 0 and available <= 0 else "no_position"
                    report.reject(dt=sctx.dt, symbol=symbol, reason=reason, requested=intent.sell_shares or 0)
                    return
                allowed, reason = can_sell(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                )
                if not allowed:
                    report.reject(dt=sctx.dt, symbol=symbol, reason=reason, requested=requested)
                    return
                py_ctx.sell_shares = requested
                py_ctx.sell_fill_price = PriceType.OPEN
                t1.on_sell(symbol, requested)
                return

            if intent.target_weight is not None:
                target = round_lot(float(intent.target_weight) * float(py_ctx.total_equity) / price, rule_engine.lot_size)
                if target > pos:
                    requested = target - pos
                    side = "buy"
                elif target < pos:
                    requested = min(pos - target, int(available))
                    side = "sell"
                    if requested <= 0:
                        report.reject(dt=sctx.dt, symbol=symbol, reason="t_plus_1", requested=pos - target)
                        return
                else:
                    return
            elif intent.buy_shares is not None:
                requested = int(intent.buy_shares)
                side = "buy"
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
                    report.reject(dt=sctx.dt, symbol=symbol, reason="lot_size", requested=requested)
                    return
                allowed, reason = can_buy(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                )
                if not allowed:
                    report.reject(dt=sctx.dt, symbol=symbol, reason=reason, requested=requested)
                    return
                max_cash = int(float(py_ctx.buying_power) / (price * 1.001))
                max_cash = round_lot(max_cash, rule_engine.lot_size)
                if requested > max_cash:
                    report.unfill(dt=sctx.dt, symbol=symbol, reason="cash", target=requested, actual=max_cash)
                    requested = max_cash
                if requested <= 0:
                    report.reject(dt=sctx.dt, symbol=symbol, reason="cash", requested=0)
                    return
                py_ctx.buy_shares = requested
                py_ctx.buy_fill_price = PriceType.OPEN
                t1.on_buy(symbol, requested)
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
                allowed, reason = can_sell(
                    row=row,
                    prev_close=prev_close_map.get((symbol, sctx.dt)),
                    code=symbol,
                    rules=rule_engine,
                )
                if not allowed:
                    report.reject(dt=sctx.dt, symbol=symbol, reason=reason, requested=requested)
                    return
                py_ctx.sell_shares = requested
                py_ctx.sell_fill_price = PriceType.OPEN
                t1.on_sell(symbol, requested)

        # 修复未绑定的局部变量（上面对 sell_shares 的引用）
        def exec_fn(py_ctx) -> None:
            dt = pd.Timestamp(py_ctx.dt).normalize()
            current_dt[0] = dt
            row = by_key.get((symbol, dt))
            if row is None:
                return
            t1.start_day(dt.date())
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
            result = strategy.backtest()
        except ValueError as exc:
            raise EngineError(f"PyBroker 回测失败: {exc}") from exc

        ledger_data = data[data["date"] >= pd.Timestamp(spec.start).normalize()].copy()
        ledger_result = SimpleLedger(
            initial_cash=float(spec.initial_cash),
            data=ledger_data,
            orders=result.orders,
            symbol=symbol,
        ).run()
        recon = reconcile(ledger_result.equity, getattr(result, "portfolio", None))

        selections = pd.DataFrame(columns=["date", "symbol", "score", "rank", "weight"])
        meta = {
            "engine": "pybroker",
            "pybroker_version": pybroker.__version__,
            "fill": spec.fill,
            "metrics_source": "ledger",
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
            corporate_actions=None,
            meta=meta,
        )
