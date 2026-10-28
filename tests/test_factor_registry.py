"""因子注册表 / 标签 / 别名 / DAG / 容差。"""

from __future__ import annotations

import pytest

from factors.registry import (
    ALIASES,
    FACTOR_REGISTRY,
    list_factors,
    register_factor,
    registry_hash,
    reset_registry,
    resolve_id,
    topological_order,
    validate_registry,
)
from factors.spec import Dependency, FactorSpec, SourceSpec, Tolerance, close_enough
from factors.taxonomy import Category


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def make_spec(factor_id="momentum.ret", **overrides):
    payload = dict(
        factor_id=factor_id,
        name="20日动量",
        category=Category.MOMENTUM,
        subcategory="return",
        source=SourceSpec("ohlcv_1d", "questdb", ("close",)),
        window=20,
        freq="1d",
        value_kind="daily",
        update_mode="shift",
        unit="ratio",
        direction="positive",
        price_basis="qfq",
        version="v1",
        source_version_policy="adj_factor",
        dependencies=(Dependency("raw", "close"),),
        warmup=20,
        aliases=("mom20",),
    )
    payload.update(overrides)
    return FactorSpec(**payload)


def test_spec_tags_contain_three_groups():
    spec = make_spec()
    tags = spec.tags()
    assert tags["category"] == "momentum"
    assert tags["source_dataset"] == "ohlcv_1d"
    assert tags["window"] == 20 and tags["freq"] == "1d"


def test_register_and_list_by_category():
    register_factor(make_spec())
    register_factor(
        make_spec(
            factor_id="technical.atr",
            name="ATR",
            category=Category.TECHNICAL,
            subcategory="atr",
            window=14,
            warmup=14,
            aliases=("atr14",),
        )
    )
    moments = list_factors(category="momentum")
    assert [item.factor_id for item in moments] == ["momentum.ret"]
    assert len(list_factors()) == 2
    assert list_factors(source_dataset="ohlcv_1d", freq="1d", window=14)[0].factor_id == (
        "technical.atr"
    )


def test_duplicate_canonical_rejected():
    register_factor(make_spec())
    with pytest.raises(ValueError):
        register_factor(make_spec())


def test_alias_resolution_and_conflict():
    register_factor(make_spec())
    assert resolve_id("mom20") == ("momentum.ret", 20)
    assert resolve_id("momentum.ret") == ("momentum.ret", 20)
    assert resolve_id("mom20", window=60) == ("momentum.ret", 60)
    with pytest.raises(ValueError):
        register_factor(make_spec(factor_id="value.ep", aliases=("mom20",)))


def test_alias_cannot_be_canonical():
    with pytest.raises(ValueError):
        register_factor(make_spec(factor_id="value.ep", aliases=("value.ep",)))


def test_cycle_detection():
    register_factor(
        make_spec(
            factor_id="momentum.a",
            dependencies=(Dependency("factor", "momentum.b"),),
            aliases=(),
        )
    )
    register_factor(
        make_spec(
            factor_id="momentum.b",
            dependencies=(Dependency("factor", "momentum.a"),),
            aliases=(),
        )
    )
    with pytest.raises(ValueError):
        topological_order()
    assert any("环" in item for item in validate_registry())


def test_topological_order_respects_dependencies():
    register_factor(
        make_spec(
            factor_id="momentum.slow",
            dependencies=(Dependency("factor", "momentum.fast"),),
            aliases=(),
        )
    )
    register_factor(make_spec(factor_id="momentum.fast", aliases=()))
    order = topological_order()
    assert order.index("momentum.fast") < order.index("momentum.slow")


def test_registry_hash_changes_with_version():
    register_factor(make_spec())
    first = registry_hash()
    register_factor(make_spec(version="v2"), replace=True)
    second = registry_hash()
    assert first != second


def test_relative_tolerance_for_large_values():
    tol = Tolerance()
    # 市值 1e12 量级：绝对差 100 在相对 1e-9 下应通过
    assert close_enough(1_000_000_000_000.0, 1_000_000_000_100.0, tol)
    # 相对差 1e-6 应失败
    assert not close_enough(1.0, 1.000001, tol)
    # NaN 只能和 NaN 相等
    assert close_enough(float("nan"), float("nan"), tol)
    assert not close_enough(float("nan"), 1.0, tol)


def test_event_factor_requires_available_at():
    with pytest.raises(ValueError):
        make_spec(
            factor_id="value.ep",
            category=Category.VALUE,
            subcategory="earnings",
            value_kind="event",
            source=SourceSpec("fin_indicator", "tushare", ("pe_ttm",), as_of="trade_date"),
        )
    spec = make_spec(
        factor_id="value.ep",
        category=Category.VALUE,
        subcategory="earnings",
        value_kind="event",
        source=SourceSpec("fin_indicator", "tushare", ("pe_ttm",), as_of="available_at"),
    )
    assert spec.value_kind == "event"


def test_invalid_subcategory_rejected():
    with pytest.raises(ValueError):
        make_spec(subcategory="not_a_subcategory")
