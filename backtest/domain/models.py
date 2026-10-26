"""回测领域模型。

这里只放跨层共享的数据结构，不 import pybroker / QuestDB / data_interface。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import pandas as pd

Mode = Literal["single", "cross"]
FillMode = Literal["next_open", "close"]
MetricsSource = Literal["ledger", "pybroker"]
CorporateActionMode = Literal["off", "approx"]


@dataclass(frozen=True)
class TradingCalendar:
    """回测使用的交易日序列。"""

    dates: tuple[pd.Timestamp, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dates",
            tuple(pd.Timestamp(item).normalize() for item in self.dates),
        )

    def __len__(self) -> int:
        return len(self.dates)

    def __iter__(self):
        return iter(self.dates)

    def index(self, dt: pd.Timestamp) -> int:
        return self.dates.index(pd.Timestamp(dt).normalize())


@dataclass(frozen=True)
class DataSnapshot:
    source: str
    max_ts: pd.Timestamp
    rows: int
    anchor_by_symbol: Mapping[str, float] = field(default_factory=dict)
    hash: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "max_ts": str(self.max_ts),
            "rows": self.rows,
            "anchors": {key: round(float(value), 8) for key, value in sorted(self.anchor_by_symbol.items())},
            "hash": self.hash,
        }


@dataclass(frozen=True)
class Universe:
    codes: tuple[str, ...]
    as_of: date
    fingerprint: str

    @classmethod
    def from_codes(cls, codes: Sequence[str], as_of: date, fingerprint: str) -> "Universe":
        return cls(codes=tuple(sorted(set(codes))), as_of=as_of, fingerprint=fingerprint)

    def is_member(self, code: str, day: date | None = None) -> bool:
        # MVP：显式 universe 在整段区间内不变。
        return code in self.codes


@dataclass(frozen=True)
class DataBundle:
    frame: pd.DataFrame
    calendar: TradingCalendar
    snapshot: DataSnapshot
    universe: Universe


@dataclass(frozen=True)
class ExecutionRules:
    lot_size: int = 100
    t_plus_1: bool = True
    limit_up_down: bool = True
    suspension: bool = True
    new_listing_no_limit_days: int = 5
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.00001
    stamp_tax_schedule: tuple[tuple[str, float], ...] = (
        ("1990-01-01", 0.001),
        ("2023-08-28", 0.0005),
    )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ExecutionRules":
        payload = dict(payload or {})
        schedule = payload.pop("stamp_tax_schedule", None)
        rules = cls(**payload)
        if schedule:
            rules = cls(
                **{
                    **payload,
                    "stamp_tax_schedule": tuple(
                        (str(item["effective_from"]), float(item["rate"]))
                        for item in schedule
                    ),
                }
            )
        return rules


@dataclass(frozen=True)
class RunSpec:
    strategy_name: str
    strategy_path: str
    mode: Mode
    symbols: tuple[str, ...]
    start: date
    end: date
    freq: str
    initial_cash: float
    params: Mapping[str, Any] = field(default_factory=dict)
    factor_source: str = "compute"
    fill: FillMode = "next_open"
    benchmark: str | None = "000300"
    rebalance: str = "weekly"
    max_gross_exposure: float = 0.98
    allow_short: bool = False
    corporate_actions: CorporateActionMode = "off"
    metrics_source: MetricsSource = "ledger"
    outputs: tuple[str, ...] = ("console",)
    data_source: str = "questdb"
    seed: int = 0
    rules: ExecutionRules = field(default_factory=ExecutionRules)


@dataclass
class ExecutionReport:
    rejected: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["date", "symbol", "reason", "requested", "filled"]
        )
    )
    unfilled: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["date", "symbol", "reason", "target", "actual"]
        )
    )
    corporate_actions: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["date", "symbol", "f_prev", "f_new", "ratio"]
        )
    )

    def reject(
        self,
        *,
        dt: Any,
        symbol: str,
        reason: str,
        requested: float = 0.0,
        filled: float = 0.0,
    ) -> None:
        self.rejected.loc[len(self.rejected)] = [dt, symbol, reason, requested, filled]

    def unfill(
        self,
        *,
        dt: Any,
        symbol: str,
        reason: str,
        target: float = 0.0,
        actual: float = 0.0,
    ) -> None:
        self.unfilled.loc[len(self.unfilled)] = [dt, symbol, reason, target, actual]


@dataclass
class OutputArtifact:
    kind: Literal["file", "table", "console"]
    path: Path | None = None
    table: str | None = None
    rows: int | None = None


@dataclass
class BacktestResult:
    run_id: str
    spec: RunSpec
    metrics: dict[str, float]
    metrics_pybroker: dict[str, float] | None
    equity: pd.DataFrame
    orders: pd.DataFrame
    trades: pd.DataFrame
    selections: pd.DataFrame
    factor_meta: dict[str, Any]
    execution_report: ExecutionReport
    corporate_actions: pd.DataFrame | None = None
    meta: dict[str, Any] = field(default_factory=dict)
