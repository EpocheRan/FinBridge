"""复权换算的单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from data_interface.market.adjust import (
    ADJUST_HFQ,
    ADJUST_NONE,
    ADJUST_QFQ,
    apply_adjust,
    normalize_adjust,
    to_raw_price,
)


def _frame(close: list[float], factors: list[float] | None = None) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(close), freq="D")
    frame = pd.DataFrame(
        {
            "open": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "close": close,
            "volume": [1000] * len(close),
        },
        index=index,
    )
    if factors is not None:
        frame["adj_factor"] = factors
    return frame


def test_normalize_adjust_aliases():
    assert normalize_adjust(None) == ADJUST_QFQ
    assert normalize_adjust("RAW") == ADJUST_NONE
    assert normalize_adjust("前复权") == ADJUST_QFQ
    assert normalize_adjust("hfq") == ADJUST_HFQ
    with pytest.raises(ValueError):
        normalize_adjust("whatever")


def test_none_mode_keeps_raw_prices():
    frame = _frame([10.0, 11.0], [1.0, 1.1])
    result = apply_adjust(frame, ADJUST_NONE)
    assert list(result["close"]) == [10.0, 11.0]
    assert result.attrs["adjust"] == ADJUST_NONE


def test_qfq_scales_to_anchor_and_latest_equals_raw():
    # 中间除权一次：因子 1.0 -> 2.0
    frame = _frame([10.0, 20.0], [1.0, 2.0])
    result = apply_adjust(frame, ADJUST_QFQ, anchor=2.0)
    assert result["close"].iloc[0] == pytest.approx(5.0)   # 10 * 1/2
    assert result["close"].iloc[1] == pytest.approx(20.0)  # 20 * 2/2（最新价不变）
    assert "raw_close" in result.columns
    assert result["raw_close"].iloc[0] == pytest.approx(10.0)


def test_qfq_is_stable_across_query_windows():
    """同一交易日在不同查询区间必须得到同样的前复权价（锚点来自库里的最新因子）。"""
    closes = [10.0, 20.0, 20.5]
    factors = [1.0, 2.0, 2.0]
    full = _frame(closes, factors)
    window = full.iloc[:2].copy()

    full_result = apply_adjust(full, ADJUST_QFQ, anchor=2.0)
    window_result = apply_adjust(window, ADJUST_QFQ, anchor=2.0)
    assert full_result["close"].iloc[1] == pytest.approx(window_result["close"].iloc[1])


def test_hfq_multiplies_by_factor():
    frame = _frame([10.0, 10.0], [1.0, 2.0])
    result = apply_adjust(frame, ADJUST_HFQ)
    assert list(result["close"]) == [pytest.approx(10.0), pytest.approx(20.0)]


def test_missing_factor_is_reported_not_silently_treated_as_one():
    frame = _frame([10.0, 11.0])
    result = apply_adjust(frame, ADJUST_QFQ, anchor=1.0)
    assert list(result["close"]) == [10.0, 11.0]
    assert result.attrs["adjust_fallback"] == "missing_adj_factor"


def test_nan_factor_rows_keep_raw_price():
    frame = _frame([10.0, 11.0], [1.0, np.nan])
    result = apply_adjust(frame, ADJUST_QFQ, anchor=1.0)
    assert result["close"].iloc[1] == pytest.approx(11.0)


def test_apply_adjust_does_not_mutate_input():
    frame = _frame([10.0], [2.0])
    original = frame["close"].iloc[0]
    apply_adjust(frame, ADJUST_QFQ, anchor=2.0)
    assert frame["close"].iloc[0] == original


def test_to_raw_price_roundtrip():
    assert to_raw_price(5.0, 1.0, 2.0) == pytest.approx(10.0)
    assert to_raw_price(5.0, None, 2.0) == 5.0


def test_missing_factor_row_inherits_neighbour_instead_of_raw_mix():
    """因子个别缺失时不能用 1.0 兜底（会把复权价与不复权价混在同一序列）。"""
    frame = _frame([10.0, 10.0, 10.0], [1.0, np.nan, 1.1])
    result = apply_adjust(frame, ADJUST_QFQ, anchor=1.1)
    scales = (result["close"] / frame["close"]).round(6).tolist()
    # 第 1、2 行应同为 1.0/1.1；不能出现第 2 行 scale=1.0（不复权）
    assert scales[0] == pytest.approx(scales[1])
    assert scales[2] == pytest.approx(1.0)
    assert result.attrs.get("adjust_filled_rows") == 1
