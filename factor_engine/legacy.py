"""screener 旧列 -> 因子规格（迁移用，元数据 only）。"""

from __future__ import annotations

from factors.spec import FactorSpec, Materialization, SourceSpec
from factors.stored import SCREENER_DAILY_COLUMNS
from factors.taxonomy import Category

LEGACY_VERSION = "daily-v1"
LEGACY_SOURCE = "factor_daily_wide"


def legacy_specs() -> list[FactorSpec]:
    specs: list[FactorSpec] = []
    for column, meta in SCREENER_DAILY_COLUMNS.items():
        specs.append(
            FactorSpec(
                factor_id=f"technical.{column}",
                name=column,
                category=Category.TECHNICAL,
                subcategory="ma_deviation",
                source=SourceSpec(LEGACY_SOURCE, "questdb", (column,), "trade_date"),
                window=0,
                freq="1d",
                value_kind="daily",
                update_mode="shift",
                unit=meta.unit,
                direction="neutral",
                price_basis=meta.price_basis,
                version=LEGACY_VERSION,
                source_version_policy="legacy",
                dependencies=(),
                warmup=0,
                formula=f"legacy screener_daily.{column}",
                aliases=(f"legacy.{column}",),
                materialization=Materialization.STORED,
            )
        )
    return specs
