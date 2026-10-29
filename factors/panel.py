"""因子面板：长表 -> date × symbol 宽表。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd

from . import cross_section
from .errors import FactorError
from .library import CROSS_SECTION_OPS, FactorDef
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
    """计算因子图需要的最大预热窗口（含合成/中性化的传递依赖）。"""
    by_name = {item.name: item for item in defs}
    warmup = 0
    for item in defs:
        if item.op in CROSS_SECTION_OPS and item.args:
            if item.op == "composite":
                spec = item.args[0]
                for part_name, _direction, _weight in spec[0]:
                    base = by_name.get(str(part_name))
                    if base is not None:
                        warmup = max(warmup, int(base.warmup or 0))
                continue
            if item.op == "neutralize":
                spec = item.args[0]
                base = by_name.get(str(spec[0]))
                if base is not None:
                    warmup = max(warmup, int(base.warmup or 0))
                continue
            base = by_name.get(str(item.args[0]))
            warmup = max(warmup, int(base.warmup) if base else 0)
        else:
            warmup = max(warmup, int(item.warmup or item.window or 0))
    return warmup


def _slice_dates(frame: pd.DataFrame) -> pd.DatetimeIndex:
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna().dt.normalize()
    return pd.DatetimeIndex(sorted(dates.unique()))


def _stored_wide(
    long: pd.DataFrame | None,
    defn: FactorDef,
    dates: pd.DatetimeIndex,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    """从因子长表 (ts, code, factor_id, value) 透视成 date × symbol。"""
    if long is None or long.empty:
        raise FactorError(f"因子 {defn.name!r} 需要 stored 数据，但未提供")
    data = long.copy()
    if "ts" in data.columns:
        data = data.rename(columns={"ts": "date"})
    if "code" in data.columns:
        data = data.rename(columns={"code": "symbol"})
    column = defn.stored_column or defn.name
    if "factor_id" in data.columns:
        data = data[data["factor_id"].astype(str) == str(column)]
    elif column in data.columns:
        data = data.rename(columns={column: "value"})
    else:
        raise FactorError(f"stored 数据缺少 factor_id/value: {defn.name!r}")
    if data.empty:
        raise FactorError(f"stored 数据中没有因子 {column!r}")
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data = data.dropna(subset=["date", "symbol", "value"])
    wide = data.pivot_table(
        index="date", columns="symbol", values="value", aggfunc="last"
    )
    return wide.reindex(index=dates, columns=list(symbols))


def build_panel(
    frame: pd.DataFrame,
    defs: Sequence[FactorDef],
    start: date,
    end: date,
    *,
    stored: pd.DataFrame | None = None,
    reference: Mapping[str, pd.DataFrame] | None = None,
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
    base_defs = [item for item in defs if item.op not in CROSS_SECTION_OPS]
    derived_defs = [item for item in defs if item.op in CROSS_SECTION_OPS]

    values: dict[str, pd.DataFrame] = {}
    meta: dict[str, FactorMeta] = {}

    for defn in base_defs:
        if defn.source == "stored":
            if defn.op != "factor":
                raise FactorError(
                    f"因子 {defn.name!r} 使用 legacy F.stored（screener_daily 列），"
                    "回测请改用 F.factor(factor_id)"
                )
            wide = _stored_wide(stored, defn, all_dates, symbols)
            values[defn.name] = wide
            meta[defn.name] = _meta(
                defn, wide, start_ts, end_ts, source="stored", warmup=0
            )
            continue
        per_symbol: dict[str, pd.Series] = {}
        for symbol, group in data.groupby("symbol", sort=True):
            series = group.set_index("date").sort_index()
            per_symbol[str(symbol)] = clean_series(compute_series(defn, series))
        wide = pd.DataFrame(per_symbol).reindex(all_dates).sort_index()
        wide = wide.reindex(columns=list(symbols))
        values[defn.name] = wide
        meta[defn.name] = _meta(defn, wide, start_ts, end_ts)

    # 派生因子可能互相依赖（如 composite 里含 rank），按依赖顺序求解
    pending = list(derived_defs)
    while pending:
        progressed = False
        for defn in list(pending):
            deps = _derived_inputs(defn)
            if not all(dep in values for dep in deps):
                continue
            if defn.op == "rank":
                wide = cross_section.rank_pct(values[deps[0]])
            elif defn.op == "zscore":
                wide = cross_section.zscore(values[deps[0]])
            elif defn.op == "composite":
                wide = _composite_wide(defn, values, reference)
            elif defn.op == "neutralize":
                base = values[deps[0]]
                wide = _apply_neutralize(base, reference, _neutralize_by(defn), name=defn.name)
            else:  # pragma: no cover - CROSS_SECTION_OPS 已穷举
                raise FactorError(f"未知派生算子: {defn.op!r}")
            values[defn.name] = wide
            base_def = by_name.get(deps[0])
            meta[defn.name] = _meta(
                defn,
                wide,
                start_ts,
                end_ts,
                source="computed",
                warmup=int(base_def.warmup) if base_def else 0,
            )
            pending.remove(defn)
            progressed = True
        if not progressed:
            raise FactorError(
                "派生因子存在未解析的依赖（检查 inputs 是否都在 defs 里）: "
                + ", ".join(item.name for item in pending)
            )

    return FactorPanel(values=values, meta=meta, dates=all_dates, symbols=symbols)


def _derived_inputs(defn: FactorDef) -> list[str]:
    """派生因子依赖的输入名。"""
    if defn.op == "composite":
        spec = defn.args[0]
        return [str(item[0]) for item in spec[0]]
    if defn.op == "neutralize":
        spec = defn.args[0]
        return [str(spec[0])]
    if defn.args:
        return [str(defn.args[0])]
    return []


def _neutralize_by(defn: FactorDef) -> tuple[str, ...]:
    if defn.op != "neutralize":
        return ()
    spec = defn.args[0]
    return tuple(str(x) for x in (spec[1] if len(spec) > 1 else ()))


def _apply_neutralize(
    panel: pd.DataFrame,
    reference: Mapping[str, pd.DataFrame] | None,
    by: Sequence[str],
    *,
    name: str = "neutralize",
) -> pd.DataFrame:
    """按 ``by``（industry / size）做截面回归取残差；缺参照面板直接报错不静默跳过。"""
    wanted = tuple(str(x) for x in by)
    if not wanted:
        return panel
    if reference is None:
        raise FactorError(
            f"因子 {name!r} 要求中性化 {wanted}，但 build_panel 没有传入 reference"
            "（需提供 date×symbol 宽表：industry / size）"
        )
    industry = reference.get("industry")
    size = reference.get("size")
    if "industry" in wanted and industry is None:
        raise FactorError(f"因子 {name!r} 需要 industry 参照面板，未提供")
    if "size" in wanted and size is None:
        raise FactorError(f"因子 {name!r} 需要 size 参照面板，未提供")
    index, columns = panel.index, panel.columns
    industry = industry.reindex(index=index, columns=columns) if industry is not None else None
    size = size.reindex(index=index, columns=columns) if size is not None else None
    return cross_section.neutralize(panel, industry=industry, size=size)


def _composite_wide(
    defn: FactorDef,
    values: Mapping[str, pd.DataFrame],
    reference: Mapping[str, pd.DataFrame] | None,
) -> pd.DataFrame:
    """等权/加权 rank 合成；逐分量做 winsorize → neutralize → signed_rank。"""
    spec = defn.args[0]
    parts, win_q, neu_by, min_parts = spec
    panels: list[pd.DataFrame] = []
    weights: list[float] = []
    directions: list[int] = []
    for part_name, direction, weight in parts:
        base = values.get(str(part_name))
        if base is None:
            raise FactorError(f"合成因子 {defn.name!r} 缺少分量 {part_name!r}")
        data = base
        if win_q:
            data = cross_section.winsorize(data, float(win_q))
        if neu_by:
            data = _apply_neutralize(data, reference, neu_by, name=f"{defn.name}.{part_name}")
        panels.append(data)
        weights.append(float(weight))
        directions.append(int(direction))
    return cross_section.composite(
        panels, weights, directions, min_parts=int(min_parts)
    )


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
