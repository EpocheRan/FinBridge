"""情绪/资金因子：算子降级、计算口径、注册元数据。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factors import F
from factors.ops import flow


def _frame(**columns) -> pd.DataFrame:
    return pd.DataFrame(columns)


def test_missing_columns_degrade_to_nan():
    frame = _frame(close=np.linspace(10, 11, 30))
    for name, (fn, *_rest) in flow.FLOW_FACTORS.items():
        series = fn(frame)
        assert isinstance(series, pd.Series), name
        assert series.isna().all(), name


def test_margin_balance_chg_20d():
    n = 40
    rzye = pd.Series(np.linspace(100.0, 100.0 * 1.2, n))
    frame = pd.DataFrame({"margin_rzye": rzye})
    result = flow.margin_balance_chg_20d(frame)
    # 线性增长：20 日增速恒定
    assert result.iloc[:20].isna().all()
    expected = rzye.iloc[-1] / rzye.iloc[-21] - 1
    assert result.iloc[-1] == pytest.approx(expected, rel=1e-9)


def test_margin_short_ratio_uses_rqye_over_rzye():
    frame = pd.DataFrame(
        {"margin_rqye": [1.0, 2.0, 3.0], "margin_rzye": [10.0, 10.0, 0.0]}
    )
    result = flow.margin_short_ratio(frame)
    assert result.iloc[0] == pytest.approx(0.1)
    assert result.iloc[1] == pytest.approx(0.2)
    assert np.isnan(result.iloc[2])  # 分母 0 → NaN，不是 inf


def test_flow_large_net_ratio_uses_amount_units():
    frame = pd.DataFrame(
        {
            "mf_buy_lg": 30.0,
            "mf_buy_elg": 20.0,
            "mf_sell_lg": 10.0,
            "mf_sell_elg": 10.0,
            "amount": 1000.0,
        },
        index=range(25),
    )
    result = flow.flow_large_net_ratio_20d(frame)
    # (30+20-10-10)/1000 = 3%
    assert result.iloc[-1] == pytest.approx(0.03, rel=1e-9)


def test_f_ts_resolves_flow_factor():
    defn = F.ts("sentiment.margin_balance_chg_20d")
    assert defn.window == 20
    assert defn.unit == "ratio"
    assert defn.op == "sentiment.margin_balance_chg_20d"
    assert "margin_rzye" in defn.inputs


def test_flow_specs_registered_as_sentiment():
    from factor_engine.definitions import FLOW_SPECS, register_all
    from factors.registry import get

    register_all()
    for spec in FLOW_SPECS:
        assert spec.category.value == "sentiment"
        assert spec.subcategory in ("margin", "moneyflow")
        stored = get(spec.factor_id)
        assert stored.factor_id == spec.factor_id
        assert stored.source.dataset in ("margin_detail", "moneyflow_daily")


def test_extras_for_maps_factor_to_dataset():
    """因子 -> 辅助数据集的映射：漏一个，回测就会「计划为空」。"""
    from backtest.application.runner import _extras_for

    margin = _extras_for([F.ts("sentiment.margin_balance_chg_20d")])
    assert margin == ("margin_detail",)

    flow = _extras_for([F.ts("sentiment.flow_large_net_ratio_20d")])
    assert flow == ("moneyflow_daily",)

    size = _extras_for([F.ts("size.float_mv")])
    assert size == ("fin_valuation_daily",)

    both = _extras_for(
        [
            F.ts("size.float_mv"),
            F.ts("sentiment.margin_short_ratio"),
            F.vol(20),
        ]
    )
    assert both == ("fin_valuation_daily", "margin_detail")
