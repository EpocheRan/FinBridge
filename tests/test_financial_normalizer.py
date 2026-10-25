"""财务标准化回归：EBITDA 不能被无脑重算覆盖。"""

from __future__ import annotations

from datetime import datetime

import pytest

from data_interface.financial.normalizer import merge_fina_indicator


def _income(**overrides):
    row = {
        "period_end": datetime(2024, 12, 31),
        "statement_type": "IS",
        "report_type": "consolidated",
        "ebit": 80.0,
        "ebitda": 100.0,
        "depreciation_and_amortization": None,
    }
    row.update(overrides)
    return row


def _fina(**overrides):
    row = {"end_date": "20241231"}
    row.update(overrides)
    return row


def test_source_ebitda_is_preserved_when_fina_has_no_daa():
    row = _income()
    merge_fina_indicator([row], [_fina(ebitda=100.0)])
    assert row["ebitda"] == pytest.approx(100.0)


def test_source_ebitda_is_not_overwritten_by_computed_ebit_plus_daa():
    row = _income()
    merge_fina_indicator([row], [_fina(ebitda=100.0, daa=10.0)])
    assert row["ebitda"] == pytest.approx(100.0)


def test_derived_ebitda_is_recomputed_after_daa_is_merged():
    row = _income(
        ebitda=80.0,
        _ebitda_derived=True,
    )
    merge_fina_indicator([row], [_fina(daa=10.0)])
    assert row["ebitda"] == pytest.approx(90.0)


def test_source_fina_ebitda_wins_over_derived_value():
    row = _income(ebitda=80.0, _ebitda_derived=True)
    merge_fina_indicator([row], [_fina(ebitda=105.0, daa=10.0)])
    assert row["ebitda"] == pytest.approx(105.0)
