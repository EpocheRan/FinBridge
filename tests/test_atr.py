import numpy as np
import pandas as pd

from factors import ATR, IndicatorEngine, atr_wilder
from screener.domain.factors import atr_wilder as screener_atr_wilder


def _frame(rows: int = 60) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="D")
    close = np.linspace(10.0, 20.0, rows) + np.sin(np.arange(rows) / 3.0)
    return pd.DataFrame(
        {
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(rows, 1000.0),
        },
        index=index,
    )


def test_factor_engine_matches_wilder_atr():
    frame = _frame()
    result = IndicatorEngine().calculate(frame, ATR, N=14, method="WILDER")

    assert list(result.columns) == ["atr"]
    expected = screener_atr_wilder(
        frame["high"], frame["low"], frame["close"], window=14
    )
    assert np.allclose(result["atr"].to_numpy(), expected.to_numpy(), equal_nan=True)


def test_atr_class_default_ema_is_finite():
    frame = _frame()
    values = ATR(N=14).calculate(frame)
    assert values.name == "atr"
    assert np.isfinite(values.iloc[-1])
    assert values.iloc[:13].isna().all()


def test_atr_wilder_helper_name_is_available():
    frame = _frame()
    values = atr_wilder(frame["high"], frame["low"], frame["close"], window=14)
    assert np.isfinite(values.iloc[-1])
