"""23 个 screener legacy 列的因子定义与计算。

实现复用 ``factor_engine.wide.FactorEngine``：一次算出 56 列宽表，
再按 factor_id 取对应列写长表。数据来源：
- 日线/复权：``ohlcv_1d``
- 基准：``ohlcv_1d`` 的 IDX（默认 000300）
- 股本/市值：``stock_basic_info``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from factor_engine.legacy import legacy_specs
from factor_engine.wide import DailyFactorConfig, FactorEngine
from factors.spec import FactorSpec

from .price import BuiltinFactor, builtin_map

LEGACY_SPECS: tuple[FactorSpec, ...] = tuple(legacy_specs())
LEGACY_COLUMNS: dict[str, str] = {
    spec.factor_id: spec.source.fields[0] for spec in LEGACY_SPECS
}


@dataclass
class LegacyComputer:
    data: object
    config: dict | None = None

    def compute(
        self,
        raw: pd.DataFrame,
        codes: Sequence[str],
        start_date,
        end_date,
    ) -> pd.DataFrame:
        benchmark = self.data.load_benchmark("000300", start_date, end_date)
        reference = self.data.load_reference(codes)
        engine = FactorEngine(DailyFactorConfig())
        return engine.compute_daily(raw, benchmark, reference)


def legacy_builtins() -> dict[str, BuiltinFactor]:
    """为兼容统一接口，返回 factor_id -> 占位列选择器。"""
    result: dict[str, BuiltinFactor] = {}
    for spec in LEGACY_SPECS:
        column = LEGACY_COLUMNS[spec.factor_id]

        def _compute(df: pd.DataFrame, column: str = column) -> pd.Series:
            return df[column] if column in df.columns else pd.Series(
                float("nan"), index=df.index
            )

        result[spec.factor_id] = BuiltinFactor(spec=spec, compute=_compute)
    return result


def all_builtin_map() -> dict[str, BuiltinFactor]:
    merged = dict(builtin_map())
    merged.update(legacy_builtins())
    return merged
