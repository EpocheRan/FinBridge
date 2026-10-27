"""A 股执行规则：整手、T+1、涨跌停、费用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from backtest.domain.models import ExecutionRules
from data_interface.common.security_code import board_of


def round_lot(shares: float, lot: int = 100) -> int:
    lot = max(1, int(lot))
    return max(0, int(shares) // lot * lot)


def limit_pct(code: str, *, is_st: bool = False, new_listing: bool = False) -> float | None:
    if new_listing:
        return None
    board = board_of(code)
    if board == "BSE":
        return 0.30
    if board in ("GEM", "STAR"):
        # 注册制后创业板/科创板 ST 仍为 20%，不能套用主板的 5%。
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def limit_prices(
    prev_close: float,
    code: str,
    *,
    is_st: bool = False,
    new_listing: bool = False,
) -> tuple[float | None, float | None]:
    pct = limit_pct(code, is_st=is_st, new_listing=new_listing)
    if pct is None or not prev_close or prev_close <= 0:
        return None, None
    return (
        round(float(prev_close) * (1.0 - pct), 2),
        round(float(prev_close) * (1.0 + pct), 2),
    )


def is_suspended(row: Any) -> bool:
    try:
        volume = float(row["volume"])
        close = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return True
    return not (volume > 0) or pd.isna(close) or close <= 0


def can_buy(
    *,
    row: Any,
    prev_close: float | None,
    code: str,
    rules: ExecutionRules,
    is_st: bool = False,
    new_listing: bool = False,
) -> tuple[bool, str]:
    if rules.suspension and is_suspended(row):
        return False, "suspended"
    if rules.limit_up_down and prev_close:
        _, upper = limit_prices(prev_close, code, is_st=is_st, new_listing=new_listing)
        if upper is not None and float(row["close"]) >= upper - 1e-9:
            return False, "limit_up"
    return True, ""


def can_sell(
    *,
    row: Any,
    prev_close: float | None,
    code: str,
    rules: ExecutionRules,
    is_st: bool = False,
    new_listing: bool = False,
) -> tuple[bool, str]:
    if rules.suspension and is_suspended(row):
        return False, "suspended"
    if rules.limit_up_down and prev_close:
        lower, _ = limit_prices(prev_close, code, is_st=is_st, new_listing=new_listing)
        if lower is not None and float(row["close"]) <= lower + 1e-9:
            return False, "limit_down"
    return True, ""


@dataclass
class TPlusOneLedger:
    enabled: bool = True
    _available: dict[str, float] = field(default_factory=dict)
    _today_bought: dict[str, float] = field(default_factory=dict)
    _last_date: date | None = None

    def start_day(self, dt: date) -> None:
        if not self.enabled:
            return
        if self._last_date == dt:
            return
        for symbol, shares in self._today_bought.items():
            self._available[symbol] = self._available.get(symbol, 0.0) + shares
        self._today_bought.clear()
        self._last_date = dt

    def available(self, symbol: str) -> float:
        """已解禁（昨日及以前买入）的股数。"""
        if not self.enabled:
            return float("inf")
        return float(self._available.get(symbol, 0.0))

    def sellable(self, symbol: str) -> float:
        """可用于**下一根 bar 卖出**的股数。

        卖单在下一根 bar 成交，所以今日成交的买入也满足 T+1
        （今日买入、次日卖出）。真正的“今日已解禁”仍看 ``available()``。
        """
        if not self.enabled:
            return float("inf")
        return float(self._available.get(symbol, 0.0)) + float(
            self._today_bought.get(symbol, 0.0)
        )

    def on_buy(self, symbol: str, shares: float) -> None:
        """买入**成交**时调用（不是下单时）。"""
        if not self.enabled:
            return
        self._today_bought[symbol] = self._today_bought.get(symbol, 0.0) + float(shares)

    def on_sell(self, symbol: str, shares: float) -> None:
        """卖出**成交**时调用：先扣已解禁，再扣今日买入。"""
        if not self.enabled:
            return
        remaining = float(shares)
        available = float(self._available.get(symbol, 0.0))
        taken = min(available, remaining)
        self._available[symbol] = max(0.0, available - taken)
        remaining -= taken
        if remaining > 0:
            today = float(self._today_bought.get(symbol, 0.0))
            self._today_bought[symbol] = max(0.0, today - min(today, remaining))


class FeeModel:
    def __init__(self, rules: ExecutionRules):
        self.rules = rules
        self._schedule = sorted(
            (pd.Timestamp(item[0]).date(), float(item[1]))
            for item in rules.stamp_tax_schedule
        )

    def stamp_rate(self, dt: date | pd.Timestamp | None) -> float:
        if not self._schedule:
            return 0.0
        day = pd.Timestamp(dt).date() if dt is not None else self._schedule[-1][0]
        rate = self._schedule[0][1]
        for effective, value in self._schedule:
            if day >= effective:
                rate = value
            else:
                break
        return rate

    def fee(
        self,
        *,
        shares: float,
        price: float,
        side: str,
        dt: date | pd.Timestamp | None,
    ) -> float:
        value = abs(float(shares) * float(price))
        if value <= 0:
            return 0.0
        commission = max(value * self.rules.commission_rate, self.rules.min_commission)
        transfer = value * self.rules.transfer_fee_rate
        stamp = value * self.stamp_rate(dt) if str(side).lower() == "sell" else 0.0
        return float(commission + transfer + stamp)
