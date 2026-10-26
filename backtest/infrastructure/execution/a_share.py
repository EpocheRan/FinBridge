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
    if is_st:
        return 0.05
    board = board_of(code)
    if board == "BSE":
        return 0.30
    if board in ("GEM", "STAR"):
        return 0.20
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
        if not self.enabled:
            return float("inf")
        return float(self._available.get(symbol, 0.0))

    def on_buy(self, symbol: str, shares: float) -> None:
        if not self.enabled:
            return
        self._today_bought[symbol] = self._today_bought.get(symbol, 0.0) + float(shares)

    def on_sell(self, symbol: str, shares: float) -> None:
        if not self.enabled:
            return
        self._available[symbol] = max(0.0, self._available.get(symbol, 0.0) - float(shares))


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
