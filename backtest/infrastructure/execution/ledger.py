"""单标的回测账本。

MVP 不处理公司行为：只回放成交、费用、现金和持仓，按不复权 close 估值。
后续接公司行为时，在这里增加 factor 变动调整。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class LedgerResult:
    equity: pd.DataFrame
    metrics: dict[str, float]
    reconciliation: dict[str, float]


class SimpleLedger:
    def __init__(
        self,
        *,
        initial_cash: float,
        data: pd.DataFrame,
        orders: pd.DataFrame,
        symbol: str,
    ):
        self.initial_cash = float(initial_cash)
        self.data = data.copy()
        self.orders = orders.copy()
        self.symbol = symbol

    def run(self) -> LedgerResult:
        data = self.data.copy()
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
        data = (
            data[data["symbol"] == self.symbol]
            .dropna(subset=["date"])
            .sort_values("date")
            .drop_duplicates("date", keep="last")
        )
        if data.empty:
            empty = pd.DataFrame(columns=["date", "cash", "market_value", "equity", "drawdown"])
            return LedgerResult(empty, {}, {})

        orders = self.orders.copy()
        if "date" in orders.columns:
            orders["date"] = pd.to_datetime(orders["date"], errors="coerce").dt.normalize()
        else:
            orders["date"] = pd.NaT
        if "id" not in orders.columns:
            orders = orders.reset_index().rename(columns={"index": "id"})

        by_date: dict[pd.Timestamp, pd.DataFrame] = {}
        for dt, group in orders.dropna(subset=["date"]).groupby("date", sort=True):
            by_date[pd.Timestamp(dt).normalize()] = group.sort_values("id")

        cash = self.initial_cash
        shares = 0.0
        last_close = float("nan")
        rows: list[dict[str, Any]] = []

        for _, bar in data.iterrows():
            dt = pd.Timestamp(bar["date"]).normalize()
            close = float(bar["close"]) if pd.notna(bar["close"]) else last_close
            if np.isfinite(close) and close > 0:
                last_close = close
            for _, order in by_date.get(dt, pd.DataFrame()).iterrows():
                qty = float(order.get("shares") or 0.0)
                price = float(order.get("fill_price") or 0.0)
                fees = float(order.get("fees") or 0.0)
                if qty <= 0 or price <= 0:
                    continue
                if str(order.get("type")).lower() == "buy":
                    cash -= qty * price + fees
                    shares += qty
                else:
                    cash += qty * price - fees
                    shares -= qty
            market_value = shares * last_close if np.isfinite(last_close) else 0.0
            equity = cash + market_value
            rows.append(
                {
                    "date": dt,
                    "cash": cash,
                    "market_value": market_value,
                    "equity": equity,
                }
            )

        frame = pd.DataFrame(rows)
        if frame.empty:
            return LedgerResult(frame, {}, {})
        frame["drawdown"] = frame["equity"] / frame["equity"].cummax() - 1.0
        metrics = self._metrics(frame)
        return LedgerResult(frame, metrics, {})

    def _metrics(self, frame: pd.DataFrame) -> dict[str, float]:
        equity = frame["equity"].astype("float64")
        if equity.empty or equity.iloc[0] <= 0:
            return {}
        returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        total = float(equity.iloc[-1] / self.initial_cash - 1.0)
        periods = max(1, len(equity) - 1)
        annualized = float((1.0 + total) ** (252.0 / periods) - 1.0) if total > -1 else -1.0
        vol = float(returns.std(ddof=1) * np.sqrt(252.0)) if len(returns) > 1 else 0.0
        sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(252.0)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
        return {
            "initial_cash": float(self.initial_cash),
            "final_equity": float(equity.iloc[-1]),
            "total_return_pct": total * 100.0,
            "annualized_return_pct": annualized * 100.0,
            "max_drawdown_pct": float(frame["drawdown"].min() * 100.0),
            "volatility_pct": vol * 100.0,
            "sharpe": sharpe,
        }


def reconcile(equity: pd.DataFrame, pybroker_portfolio: pd.DataFrame | None) -> dict[str, float]:
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
    return {
        "max_abs_diff": float(diff.max()),
        "mean_abs_diff": float(diff.mean()),
        "points": float(len(joined)),
    }
