"""回测账本：多标的回放 + 公司行为钩子。

- 按不复权 ``close`` 估值；停牌日沿用最近收盘。
- 只回放**成交**（PyBroker orders），不预测订单。
- 公司行为：``corporate_actions`` 为 ``date/symbol/cash_div/share_ratio`` 表，
  按除权日调整现金/股数；``approx`` 模式由 ``adj_factor`` 推导，只做近似。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass
class LedgerResult:
    equity: pd.DataFrame
    metrics: dict[str, float]
    reconciliation: dict[str, float]


def _benchmark_metrics(
    equity_frame: pd.DataFrame,
    benchmark: pd.Series | None,
    risk_free_rate: float,
) -> dict[str, float]:
    if benchmark is None or benchmark.empty or equity_frame.empty:
        return {}
    frame = equity_frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=["date"]).set_index("date")
    bench = benchmark.copy()
    bench.index = pd.to_datetime(bench.index, errors="coerce").normalize()
    bench = pd.to_numeric(bench, errors="coerce").dropna().sort_index()
    if bench.empty:
        return {}
    bench = bench.reindex(frame.index).ffill()
    joined = pd.DataFrame(
        {
            "portfolio": frame["equity"].astype("float64"),
            "benchmark": bench.astype("float64"),
        }
    ).dropna()
    if len(joined) < 2:
        return {}
    port_ret = joined["portfolio"].pct_change().dropna()
    bench_ret = joined["benchmark"].pct_change().dropna()
    aligned = pd.concat([port_ret.rename("p"), bench_ret.rename("b")], axis=1).dropna()
    if len(aligned) < 2 or aligned["b"].var(ddof=1) <= 0:
        return {}
    beta = float(aligned["p"].cov(aligned["b"]) / aligned["b"].var(ddof=1))
    rf_daily = float(risk_free_rate) / 252.0
    alpha_daily = float(
        aligned["p"].mean() - rf_daily - beta * (aligned["b"].mean() - rf_daily)
    )
    active = aligned["p"] - aligned["b"]
    tracking_error = float(active.std(ddof=1) * np.sqrt(252.0))
    information_ratio = (
        float(active.mean() / active.std(ddof=1) * np.sqrt(252.0))
        if active.std(ddof=1) > 0
        else 0.0
    )
    bench_total = float(joined["benchmark"].iloc[-1] / joined["benchmark"].iloc[0] - 1.0)
    port_total = float(joined["portfolio"].iloc[-1] / joined["portfolio"].iloc[0] - 1.0)
    return {
        "beta": beta,
        "alpha_annualized_pct": alpha_daily * 252.0 * 100.0,
        "correlation": float(aligned["p"].corr(aligned["b"])),
        "tracking_error_pct": tracking_error * 100.0,
        "information_ratio": information_ratio,
        "benchmark_return_pct": bench_total * 100.0,
        "excess_return_pct": (port_total - bench_total) * 100.0,
        "benchmark_points": float(len(joined)),
    }


def _metrics(
    equity_frame: pd.DataFrame,
    initial_cash: float,
    benchmark: pd.Series | None = None,
    risk_free_rate: float = 0.0,
) -> dict[str, float]:
    equity = equity_frame["equity"].astype("float64")
    if equity.empty or equity.iloc[0] <= 0:
        return {}
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    total = float(equity.iloc[-1] / initial_cash - 1.0)
    periods = max(1, len(equity) - 1)
    annualized = float((1.0 + total) ** (252.0 / periods) - 1.0) if total > -1 else -1.0
    vol = float(returns.std(ddof=1) * np.sqrt(252.0)) if len(returns) > 1 else 0.0
    rf_daily = float(risk_free_rate) / 252.0
    sharpe = (
        float((returns.mean() - rf_daily) / returns.std(ddof=1) * np.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    result = {
        "initial_cash": float(initial_cash),
        "final_equity": float(equity.iloc[-1]),
        "total_return_pct": total * 100.0,
        "annualized_return_pct": annualized * 100.0,
        "max_drawdown_pct": float(equity_frame["drawdown"].min() * 100.0),
        "volatility_pct": vol * 100.0,
        "sharpe": sharpe,
    }
    result.update(_benchmark_metrics(equity_frame, benchmark, risk_free_rate))
    return result


class PortfolioLedger:
    """多标的账本：现金 + 各标的股数，按日 mark-to-market。"""

    def __init__(
        self,
        *,
        initial_cash: float,
        data: pd.DataFrame,
        orders: pd.DataFrame,
        symbols: Sequence[str] | None = None,
        corporate_actions: pd.DataFrame | None = None,
        benchmark: pd.Series | None = None,
        risk_free_rate: float = 0.0,
    ):
        self.initial_cash = float(initial_cash)
        self.data = data.copy()
        self.orders = orders.copy()
        self.symbols = tuple(symbols) if symbols is not None else None
        self.corporate_actions = corporate_actions
        self.benchmark = benchmark
        self.risk_free_rate = float(risk_free_rate)

    def run(self) -> LedgerResult:
        data = self.data.copy()
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
        data = data.dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
        if data.empty:
            empty = pd.DataFrame(
                columns=["date", "cash", "market_value", "equity", "drawdown"]
            )
            return LedgerResult(empty, {}, {})

        symbols = tuple(self.symbols or sorted(data["symbol"].astype(str).unique()))
        dates = tuple(sorted(data["date"].unique()))
        bars_by_date = {dt: group for dt, group in data.groupby("date", sort=True)}
        shares: dict[str, float] = {symbol: 0.0 for symbol in symbols}
        last_close: dict[str, float] = {symbol: float("nan") for symbol in symbols}
        cash = self.initial_cash

        orders = self.orders.copy()
        if "date" not in orders.columns:
            orders["date"] = pd.NaT
        orders["date"] = pd.to_datetime(orders["date"], errors="coerce").dt.normalize()
        if "id" not in orders.columns:
            orders = orders.reset_index().rename(columns={"index": "id"})
        orders_by_date = {
            pd.Timestamp(dt).normalize(): group.sort_values("id")
            for dt, group in orders.dropna(subset=["date"]).groupby("date", sort=True)
        }

        rows: list[dict[str, Any]] = []
        for raw_dt in dates:
            dt = pd.Timestamp(raw_dt).normalize()
            for _, bar in bars_by_date.get(raw_dt, pd.DataFrame()).iterrows():
                symbol = str(bar["symbol"])
                close = pd.to_numeric(bar["close"], errors="coerce")
                if pd.notna(close) and float(close) > 0:
                    last_close[symbol] = float(close)

            cash = self._apply_corporate_actions(dt, shares, cash)

            for _, order in orders_by_date.get(dt, pd.DataFrame()).iterrows():
                symbol = str(order.get("symbol"))
                qty = float(order.get("shares") or 0.0)
                price = float(order.get("fill_price") or 0.0)
                fees = float(order.get("fees") or 0.0)
                if qty <= 0 or price <= 0:
                    continue
                if str(order.get("type")).lower() == "buy":
                    cash -= qty * price + fees
                    shares[symbol] = shares.get(symbol, 0.0) + qty
                else:
                    cash += qty * price - fees
                    shares[symbol] = shares.get(symbol, 0.0) - qty

            market_value = 0.0
            for symbol, qty in shares.items():
                if qty == 0:
                    continue
                close = last_close.get(symbol, float("nan"))
                if np.isfinite(close):
                    market_value += qty * close
            rows.append(
                {
                    "date": dt,
                    "cash": cash,
                    "market_value": market_value,
                    "equity": cash + market_value,
                }
            )

        frame = pd.DataFrame(rows)
        if frame.empty:
            return LedgerResult(frame, {}, {})
        frame["drawdown"] = frame["equity"] / frame["equity"].cummax() - 1.0
        return LedgerResult(
            frame,
            _metrics(
                frame,
                self.initial_cash,
                benchmark=self.benchmark,
                risk_free_rate=self.risk_free_rate,
            ),
            {},
        )

    # ------------------------------------------------------------------ 公司行为
    def _apply_corporate_actions(
        self,
        dt: pd.Timestamp,
        shares: dict[str, float],
        cash: float,
    ) -> float:
        """公司行为扩展点（v1 未启用）。

        ``corporate_actions`` 约定列：
        ``date, symbol, cash_div, share_ratio``。
        返回调整后的现金（当前未启用，原样返回）。
        """
        if self.corporate_actions is None or self.corporate_actions.empty:
            return cash
        frame = self.corporate_actions
        frame = frame[pd.to_datetime(frame["date"]).dt.normalize() == dt]
        for _, action in frame.iterrows():
            symbol = str(action["symbol"])
            qty = shares.get(symbol, 0.0)
            if qty <= 0:
                continue
            cash += qty * float(action.get("cash_div") or 0.0)
            ratio = float(action.get("share_ratio") or 0.0)
            if ratio:
                shares[symbol] = qty * (1.0 + ratio)
        return cash


class SimpleLedger:
    """单标的账本的兼容包装。"""

    def __init__(
        self,
        *,
        initial_cash: float,
        data: pd.DataFrame,
        orders: pd.DataFrame,
        symbol: str,
        benchmark: pd.Series | None = None,
        risk_free_rate: float = 0.0,
        corporate_actions: pd.DataFrame | None = None,
    ):
        self.initial_cash = float(initial_cash)
        self.data = data.copy()
        self.orders = orders.copy()
        self.symbol = symbol
        self.benchmark = benchmark
        self.risk_free_rate = float(risk_free_rate)
        self.corporate_actions = corporate_actions

    def run(self) -> LedgerResult:
        return PortfolioLedger(
            initial_cash=self.initial_cash,
            data=self.data,
            orders=self.orders,
            symbols=(self.symbol,),
            benchmark=self.benchmark,
            risk_free_rate=self.risk_free_rate,
            corporate_actions=self.corporate_actions,
        ).run()


def reconcile(
    equity: pd.DataFrame,
    pybroker_portfolio: pd.DataFrame | None,
    *,
    tolerance: float | None = None,
) -> dict[str, float]:
    """账本 vs PyBroker portfolio 对账；``tolerance`` 为相对阈值。"""
    if pybroker_portfolio is None or pybroker_portfolio.empty or equity.empty:
        return {}
    left = equity.set_index("date")["equity"].astype("float64")
    right = pybroker_portfolio.copy()
    if "date" not in right.columns:
        right = right.reset_index()
        right = right.rename(columns={right.columns[0]: "date"})
    right["date"] = pd.to_datetime(right["date"], errors="coerce").dt.normalize()
    right = right.set_index("date")["equity"].astype("float64")
    joined = pd.concat([left.rename("ledger"), right.rename("pybroker")], axis=1).dropna()
    if joined.empty:
        return {}
    diff = (joined["ledger"] - joined["pybroker"]).abs()
    scale = joined["pybroker"].abs().replace(0.0, np.nan)
    relative = (diff / scale).replace([np.inf, -np.inf], np.nan)
    result = {
        "max_abs_diff": float(diff.max()),
        "mean_abs_diff": float(diff.mean()),
        "max_rel_diff": float(relative.max()) if relative.notna().any() else 0.0,
        "points": float(len(joined)),
    }
    if tolerance is not None:
        result["exceeds_tolerance"] = float(result["max_rel_diff"] > tolerance)
    return result
