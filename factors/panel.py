"""因子面板：长表 -> date × symbol 宽表。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

import pandas as pd

from . import cross_section
from .errors import FactorError
from .library import FactorDef
from .ops import clean_series, compute_series


@dataclass(frozen=True)
class FactorMeta:
    name: str
    source: str
    unit: str
    version: str
    warmup: int
    coverage_start: date | None
    coverage_end: date | None
    missing_rate: float
    price_basis: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "unit": self.unit,
            "version": self.version,
            "warmup": self.warmup,
            "coverage_start": None if self.coverage_start is None else str(self.coverage_start),
            "coverage_end": None if self.coverage_end is None else str(self.coverage_end),
            "missing_rate": round(float(self.missing_rate), 6),
            "price_basis": self.price_basis,
        }


@dataclass
class FactorPanel:
    values: dict[str, pd.DataFrame]
    meta: dict[str, FactorMeta]
    dates: pd.DatetimeIndex
    symbols: tuple[str, ...]

    def at(self, dt: Any, name: str) -> pd.Series:
        key = pd.Timestamp(dt).normalize()
        if name not in self.values:
            raise KeyError(f"未知因子: {name!r}")
        frame = self.values[name]
        if key not in frame.index:
            return pd.Series(dtype="float64", name=name)
        return frame.loc[key]

    def get(self, symbol: str, name: str, dt: Any) -> float:
        row = self.at(dt, name)
        if symbol not in row.index:
            return float("nan")
        return float(row[symbol])

    def view(self, current_dt: Any) -> "FactorPanelView":
        return FactorPanelView(self, pd.Timestamp(current_dt).normalize())


class FactorPanelView:
    """只允许看 <= current_dt 的面板视图，防止策略整列回看。"""

    def __init__(self, panel: FactorPanel, current_dt: pd.Timestamp):
        self._panel = panel
        self._current = current_dt

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self._panel.dates[self._panel.dates <= self._current]

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._panel.symbols

    @property
    def current_dt(self) -> pd.Timestamp:
        return self._current

    @property
    def factor_names(self) -> tuple[str, ...]:
        return tuple(self._panel.values)

    def at(self, dt: Any, name: str) -> pd.Series:
        key = pd.Timestamp(dt).normalize()
        if key > self._current:
            raise FactorError(f"禁止读取未来因子: dt={key} > current={self._current}")
        return self._panel.at(key, name)

    def get(self, symbol: str, name: str, dt: Any) -> float:
        key = pd.Timestamp(dt).normalize()
        if key > self._current:
            raise FactorError(f"禁止读取未来因子: dt={key} > current={self._current}")
        return self._panel.get(symbol, name, key)


def required_warmup(defs: Sequence[FactorDef]) -> int:
    """计算因子图需要的最大预热窗口。"""
    by_name = {item.name: item for item in defs}
    warmup = 0
    for item in defs:
        if item.op in ("rank", "zscore") and item.args:
            base = by_name.get(str(item.args[0]))
            warmup = max(warmup, int(base.warmup) if base else 0)
        else:
            warmup = max(warmup, int(item.warmup or item.window or 0))
    return warmup


def _slice_dates(frame: pd.DataFrame) -> pd.DatetimeIndex:
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna().dt.normalize()
    return pd.DatetimeIndex(sorted(dates.unique()))


def build_panel(
    frame: pd.DataFrame,
    defs: Sequence[FactorDef],
    start: date,
    end: date,
) -> FactorPanel:
    if frame.empty:
        raise FactorError("因子面板输入为空")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data = data.dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
    symbols = tuple(sorted(data["symbol"].unique()))
    all_dates = _slice_dates(data)
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()

    by_name = {item.name: item for item in defs}
    base_defs = [item for item in defs if item.op not in ("rank", "zscore")]
    derived_defs = [item for item in defs if item.op in ("rank", "zscore")]

    values: dict[str, pd.DataFrame] = {}
    meta: dict[str, FactorMeta] = {}

    for defn in base_defs:
        if defn.source == "stored":
            raise FactorError(
                f"因子 {defn.name!r} 使用 stored，但当前 MVP 只支持 compute；"
                "stored 将在 screener_daily 接入阶段实现"
            )
        per_symbol: dict[str, pd.Series] = {}
        for symbol, group in data.groupby("symbol", sort=True):
            series = group.set_index("date").sort_index()
            per_symbol[str(symbol)] = clean_series(compute_series(defn, series))
        wide = pd.DataFrame(per_symbol).reindex(all_dates).sort_index()
        values[defn.name] = wide
        meta[defn.name] = _meta(defn, wide, start_ts, end_ts)

    for defn in derived_defs:
        base_name = str(defn.args[0]) if defn.args else ""
        if base_name not in values:
            raise FactorError(
                f"派生因子 {defn.name!r} 依赖的 {base_name!r} 不在因子列表中"
            )
        base = values[base_name]
        if defn.op == "rank":
            wide = cross_section.rank_pct(base)
        elif defn.op == "zscore":
            wide = cross_section.zscore(base)
        else:
            raise FactorError(f"未知派生算子: {defn.op!r}")
        values[defn.name] = wide
        base_def = by_name.get(base_name)
        meta[defn.name] = _meta(
            defn,
            wide,
            start_ts,
            end_ts,
            source="computed",
            warmup=int(base_def.warmup) if base_def else 0,
        )

    return FactorPanel(values=values, meta=meta, dates=all_dates, symbols=symbols)


def _meta(
    defn: FactorDef,
    wide: pd.DataFrame,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    *,
    source: str = "computed",
    warmup: int | None = None,
) -> FactorMeta:
    window = wide.loc[(wide.index >= start_ts) & (wide.index <= end_ts)]
    valid = window.notna()
    coverage = valid.any(axis=1)
    first = window.index[coverage].min() if coverage.any() else None
    last = window.index[coverage].max() if coverage.any() else None
    total = int(window.size)
    missing = 1.0 - (float(valid.sum().sum()) / total) if total else 1.0
    return FactorMeta(
        name=defn.name,
        source=source,
        unit=defn.unit,
        version=defn.version,
        warmup=int(warmup if warmup is not None else defn.warmup),
        coverage_start=None if first is None else first.date(),
        coverage_end=None if last is None else last.date(),
        missing_rate=missing,
        price_basis=defn.price_basis,
    )
