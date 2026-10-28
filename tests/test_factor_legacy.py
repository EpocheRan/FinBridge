"""legacy screener 列 -> FactorSpec 元数据。"""

from __future__ import annotations

from factor_engine.legacy import legacy_specs


def test_legacy_specs_cover_screener_columns():
    specs = legacy_specs()
    assert len(specs) == 23
    ids = {spec.factor_id for spec in specs}
    assert len(ids) == len(specs)
    for spec in specs:
        assert spec.source.dataset == "factor_daily_wide"
        assert spec.version == "daily-v1"
        assert spec.freq == "1d"
        assert spec.aliases and spec.aliases[0].startswith("legacy.")


def test_legacy_ids_are_canonical():
    for spec in legacy_specs():
        assert spec.factor_id.startswith("technical.")
        assert spec.category.value == "technical"
        assert spec.subcategory == "ma_deviation"
