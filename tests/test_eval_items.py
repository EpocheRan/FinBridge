"""评估项框架：注册、依赖顺序、失败隔离、schema 推导。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.eval.data import Membership, PanelData
from research.eval_items import EvalContext, catalog, execute, resolve, schema_for


def _panel(n_symbols: int = 40, n_days: int = 90, seed: int = 5) -> PanelData:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    rows = []
    for symbol in symbols:
        price = 10.0
        for dt in dates:
            price *= 1.0 + rng.normal(0, 0.012)
            rows.append({
                "symbol": symbol, "date": dt, "open": price, "high": price * 1.01,
                "low": price * 0.99, "close": price, "hfq_open": price,
                "hfq_close": price, "volume": 1e5, "amount": 1e6, "turnover": 1.0,
                "adj_factor": 1.0,
            })
    frame = pd.DataFrame(rows)
    return PanelData(frame=frame, membership=Membership(), dates=dates,
                     codes=tuple(symbols), label="test")


def _context(panel: PanelData, factor: pd.DataFrame | None = None, direction: int = 1) -> EvalContext:
    dates = panel.dates
    symbols = list(panel.codes)
    if factor is None:
        close = panel.frame.pivot_table(index="date", columns="symbol", values="hfq_close")
        open_ = panel.frame.pivot_table(index="date", columns="symbol", values="hfq_open")
        factor = close.shift(-20) / open_.shift(-1) - 1
    forward = (
        panel.frame.pivot_table(index="date", columns="symbol", values="hfq_close").shift(-20)
        / panel.frame.pivot_table(index="date", columns="symbol", values="hfq_open").shift(-1)
        - 1
    )
    values = factor.reindex(index=dates, columns=symbols)
    return EvalContext(
        factor_id="test.factor", direction=direction, values=values,
        signal=values * direction, forward=forward, membership=panel.membership,
        panel_dates=dates, quantile=0.2, min_names=10, cost_bps=10, horizon=20,
    )


def test_catalog_contains_core_items():
    ids = set(catalog())
    assert {"coverage", "ic", "spread", "turnover", "mono", "rating", "stability_by_year"} <= ids


def test_resolve_orders_by_requires():
    ordered = [impl.spec.id for impl in resolve(["rating", "ic", "spread"])]
    assert ordered.index("ic") < ordered.index("spread") < ordered.index("rating")


def test_schema_for_is_stable_and_deduplicated():
    first = schema_for(["ic", "spread"])
    second = schema_for(["ic", "spread"])
    assert first == second
    assert "ic" in first and "spread_net" in first
    assert len(first) == len(set(first))


def test_execute_produces_values_and_isolates_failures():
    panel = _panel()
    ctx = _context(panel)
    results = execute(["coverage", "ic", "spread", "mono", "rating"], ctx)
    by_id = {r.id: r for r in results}
    assert by_id["ic"].status in {"ok", "degraded"}  # 完美因子 t 值会退化为 NaN
    assert by_id["ic"].values["ic"] > 0.5          # 用前向收益当因子，IC 应接近 1
    assert by_id["spread"].values["spread_net"] > 0
    # 评级不再在 item 内另算：无报告上下文时必须明确 unavailable
    assert by_id["rating"].status == "unavailable"


def test_reference_rho_without_reference_is_unavailable():
    panel = _panel()
    ctx = _context(panel)
    results = execute(["ic", "reference_rho"], ctx)
    by_id = {r.id: r for r in results}
    assert by_id["reference_rho"].status == "unavailable"
    assert "参照" in by_id["reference_rho"].note


def test_unknown_item_raises():
    import pytest

    with pytest.raises(KeyError):
        resolve(["not_an_item"])
