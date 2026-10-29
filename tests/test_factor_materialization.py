"""因子 materialization 标记与回测 stored/inline 解析。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.application.runner import BacktestRunner, _effective_mode
from backtest.domain.errors import ConfigError
from factor_engine.definitions import INLINE_COMPUTE_REGISTRY, register_all
from factors import F
from factors.registry import default_precompute_ids
from factors.spec import FactorSpec, Materialization, SourceSpec


def _frame(days: int = 40) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=days)
    close = 10 + np.arange(days) * 0.1
    return pd.DataFrame(
        {
            "symbol": "AAA",
            "date": dates,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000,
            "adj_factor": 1.0,
        }
    )


class FakeData:
    def __init__(self, frame):
        self.frame = frame


class FakeDS:
    def __init__(self, stored):
        self.stored = stored

    def load_factors(self, symbols, start, end, ids):
        return self.stored


def _runner(factors, stored=None, factor_source="auto"):
    runner = BacktestRunner.__new__(BacktestRunner)
    runner.data_source = FakeDS(stored)
    runner.settings = SimpleNamespace(factor_source=factor_source)
    spec = SimpleNamespace(symbols=("AAA",))
    frame = runner._build_stored_frame(
        FakeData(_frame()), factors, spec, "2026-02-01", "2026-02-20", 20
    )
    return runner, frame


def test_materialization_defaults_and_payload():
    spec = FactorSpec(
        factor_id="size.log_mv",
        name="对数市值",
        category="size",
        subcategory="total_mv",
        source=SourceSpec("ohlcv_1d", "questdb", ("close",)),
        window=1,
        freq="1d",
    )
    assert spec.materialization is Materialization.AUTO
    assert spec.to_payload()["materialization"] == "auto"
    with pytest.raises(ValueError):
        FactorSpec(
            factor_id="size.log_mv2",
            name="x",
            category="size",
            subcategory="total_mv",
            source=SourceSpec("ohlcv_1d", "questdb", ("close",)),
            window=1,
            freq="1d",
            materialization="weird",
        )


def test_f_factor_carries_materialization():
    assert F.factor("momentum.ret").materialization == "auto"
    assert F.factor("momentum.ret", materialization="inline").materialization == "inline"
    assert F.factor("momentum.ret", materialization="stored").materialization == "stored"


def test_effective_mode_resolution():
    assert _effective_mode(F.mom(20), "auto") == "inline"
    assert _effective_mode(F.factor("x"), "auto") == "auto"
    assert _effective_mode(F.factor("x"), "stored") == "stored"
    assert _effective_mode(F.factor("x"), "compute") == "inline"
    assert _effective_mode(F.factor("x", materialization="inline"), "auto") == "inline"
    assert _effective_mode(F.factor("x", materialization="stored"), "compute") == "inline"


def test_inline_compute_registry_excludes_stored_only():
    assert set(INLINE_COMPUTE_REGISTRY) == {
        "momentum.ret",
        "reversal.short",
        "technical.atr",
        "risk.vol",
    }
    assert "liquidity.turnover" not in INLINE_COMPUTE_REGISTRY


def test_default_precompute_ids_are_stored_or_auto():
    register_all()
    ids = default_precompute_ids()
    assert "momentum.ret" in ids
    assert "technical.pct_chg" in ids
    assert "growth.yoy_revenue" in ids
    assert "valuation.pe_ttm" in ids
    # 因子库扩容后（Tushare 价量 86 + 财务 62），改为结构断言：
    # 每个预计算 id 必须是 STORED/AUTO，且与 registry 中标记一致。
    from factors.registry import list_factors

    expected = {
        spec.factor_id
        for spec in list_factors()
        if spec.materialization in (Materialization.STORED, Materialization.AUTO)
    }
    assert set(ids) == expected
    assert len(ids) >= 100


def test_auto_falls_back_to_inline_when_stored_missing():
    register_all()
    _, frame = _runner(
        [F.factor("momentum.ret")],
        stored=pd.DataFrame(columns=["ts", "code", "factor_id", "value"]),
    )
    assert frame is not None and not frame.empty
    assert set(frame["factor_id"]) == {"momentum.ret"}


def test_inline_materialization_computes_bar_by_bar():
    register_all()
    _, frame = _runner([F.factor("momentum.ret", materialization="inline")])
    assert frame is not None and not frame.empty
    assert (frame["factor_id"] == "momentum.ret").all()


def test_stored_missing_without_compute_errors():
    register_all()
    with pytest.raises(ConfigError, match="STORED"):
        _runner([F.factor("technical.pct_chg", materialization="stored")])
