"""策略可见上下文：只读数据 + 订单意图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd


class PanelView(Protocol):
    def at(self, dt: Any, name: str) -> pd.Series: ...
    def get(self, symbol: str, name: str, dt: Any) -> float: ...


@dataclass
class OrderIntent:
    target_weight: float | None = None
    buy_shares: int | None = None
    sell_shares: int | None = None
    sell_percent: float | None = None
    close_position: bool = False
    stop_loss: float | None = None
    stop_loss_pct: float | None = None
    trailing_pct: float | None = None
    hold_bars: int | None = None

    def clear(self) -> None:
        self.target_weight = None
        self.buy_shares = None
        self.sell_shares = None
        self.sell_percent = None
        self.close_position = False
        self.stop_loss = None
        self.stop_loss_pct = None
        self.trailing_pct = None
        self.hold_bars = None

    @property
    def has_order(self) -> bool:
        return any(
            value is not None
            for value in (
                self.target_weight,
                self.buy_shares,
                self.sell_shares,
                self.sell_percent,
            )
        ) or self.close_position


@dataclass
class StrategyContext:
    symbol: str
    dt: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    hfq_close: float
    pos: float
    cash: float
    equity: float
    weight: float
    bars_held: int
    panel: PanelView
    rank: float | None = None
    selected: bool = False
    universe_size: int = 0
    is_rebalance: bool = False
    intent: OrderIntent = field(default_factory=OrderIntent)

    def f(self, name: str) -> float:
        return float(self.panel.get(self.symbol, name, self.dt))

    def target_weight(
        self,
        weight: float,
        *,
        stop_loss: float | None = None,
        stop_loss_pct: float | None = None,
        trailing_pct: float | None = None,
        hold_bars: int | None = None,
    ) -> None:
        if not 0.0 <= float(weight) <= 1.0:
            raise ValueError("target_weight 必须在 [0, 1]")
        self.intent.target_weight = float(weight)
        self.intent.stop_loss = None if stop_loss is None else float(stop_loss)
        self.intent.stop_loss_pct = None if stop_loss_pct is None else float(stop_loss_pct)
        self.intent.trailing_pct = None if trailing_pct is None else float(trailing_pct)
        self.intent.hold_bars = None if hold_bars is None else int(hold_bars)

    def buy_shares(self, shares: int) -> None:
        if int(shares) <= 0:
            raise ValueError("buy_shares 必须 > 0")
        self.intent.buy_shares = int(shares)

    def sell_shares(self, shares: int) -> None:
        if int(shares) <= 0:
            raise ValueError("sell_shares 必须 > 0")
        self.intent.sell_shares = int(shares)

    def sell_percent(self, percent: float) -> None:
        if not 0.0 < float(percent) <= 1.0:
            raise ValueError("sell_percent 必须在 (0, 1]")
        self.intent.sell_percent = float(percent)

    def close_position(self) -> None:
        self.intent.close_position = True

    def set_stop(
        self,
        *,
        loss: float | None = None,
        loss_pct: float | None = None,
        trailing_pct: float | None = None,
        hold_bars: int | None = None,
    ) -> None:
        self.intent.stop_loss = None if loss is None else float(loss)
        self.intent.stop_loss_pct = None if loss_pct is None else float(loss_pct)
        self.intent.trailing_pct = None if trailing_pct is None else float(trailing_pct)
        self.intent.hold_bars = None if hold_bars is None else int(hold_bars)
