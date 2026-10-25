"""筛股可查询字段白名单。

domain 只描述“哪些字段可筛/可排、允许什么操作符”，
SQL 表达式由 infrastructure/query_builder 负责映射。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidScreenSpec

NUMERIC_OPS = frozenset(
    {"eq", "ne", "gt", "ge", "lt", "le", "between", "in", "is_null", "not_null"}
)
STRING_OPS = frozenset({"eq", "ne", "in", "is_null", "not_null"})
BOOL_OPS = frozenset({"eq", "ne"})


@dataclass(frozen=True)
class FieldSpec:
    name: str
    dtype: str
    kind: str = "column"
    filterable: bool = True
    sortable: bool = True
    operators: frozenset[str] = NUMERIC_OPS


def _numeric(name: str, *, sortable: bool = True) -> FieldSpec:
    return FieldSpec(name=name, dtype="float", filterable=True, sortable=sortable)


def _boolean(name: str) -> FieldSpec:
    return FieldSpec(
        name=name,
        dtype="bool",
        filterable=True,
        sortable=False,
        operators=BOOL_OPS,
    )


FIELD_REGISTRY: dict[str, FieldSpec] = {
    "ts": FieldSpec("ts", "datetime", sortable=True, operators=STRING_OPS),
    "code": FieldSpec("code", "str", operators=STRING_OPS),
    "market": FieldSpec("market", "str", operators=STRING_OPS),
    "benchmark_code": FieldSpec("benchmark_code", "str", operators=STRING_OPS),
    "formula_version": FieldSpec("formula_version", "str", operators=STRING_OPS),
}

for _name in (
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "amount",
    "turnover",
    "pct_chg",
    "gap_pct",
    "intraday_pct",
    "range_pct",
    "avg_volume_5d",
    "avg_volume_20d",
    "avg_volume_60d",
    "avg_amount_20d",
    "volume_ratio_20d",
    "volume_chg_pct",
    "amount_ratio_20d",
    "atr14",
    "atr_pct",
    "volatility_20d",
    "volatility_60d",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "ma250",
    "close_vs_ma20_pct",
    "close_vs_ma60_pct",
    "close_vs_ma250_pct",
    "high_20d",
    "low_20d",
    "high_52w",
    "low_52w",
    "dist_high_52w_pct",
    "dist_low_52w_pct",
    "beta_252",
    "corr_252",
    "beta_window",
    "total_share",
    "float_share",
    "total_mv",
    "float_mv",
    "lookback_days",
    "computed_at",
):
    FIELD_REGISTRY[_name] = _numeric(
        _name, sortable=_name not in ("computed_at",)
    )

for _name in (
    "is_new_high_52w",
    "is_new_low_52w",
    "is_partial",
    "core_ready",
    "full_ready",
):
    FIELD_REGISTRY[_name] = _boolean(_name)

# 查询时派生的字段；不落库。
for _name in (
    "price_x_avg_volume",
    "price_x_volume",
    "pct_from_open",
    "dist_high_20d_pct",
    "dist_low_20d_pct",
):
    FIELD_REGISTRY[_name] = FieldSpec(
        name=_name,
        dtype="float",
        kind="derived",
        filterable=True,
        sortable=True,
    )

STRING_FIELDS = frozenset(
    name for name, spec in FIELD_REGISTRY.items() if spec.dtype == "str"
)


def validate_operator(op: str, field: FieldSpec, name: str) -> None:
    if op not in field.operators:
        raise InvalidScreenSpec(
            f"字段 {name} 不支持操作符 {op}，允许: {sorted(field.operators)}"
        )


def resolve_field(name: str) -> FieldSpec:
    spec = FIELD_REGISTRY.get(name)
    if spec is None:
        raise InvalidScreenSpec(f"不允许的字段: {name}")
    return spec
