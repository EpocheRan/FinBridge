import pandas as pd
import pytest

pytest.importorskip("pybroker")

from backtest.data_questdb import QuestDBDataSource


class FakeLoader:
    """只实现 ``ohlc`` 的最小 loader 桩。"""

    def ohlc(self, codes, start, end, freq="d", adjust="qfq", **kwargs):
        dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
        result = {}
        for code in codes:
            close = [10.0, 11.0] if adjust == "none" else [9.0, 9.9]
            result[code] = pd.DataFrame(
                {
                    "ts": dates,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": [100.0, 200.0],
                    "adj_factor": [1.0, 1.1],
                }
            )
        return result


def test_questdb_data_source_columns_and_adjustment():
    source = QuestDBDataSource(FakeLoader())
    frame = source.query(["600519"], "2025-01-01", "2025-01-05", timeframe="1d")

    assert list(frame.columns) == [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
    ]
    assert frame["symbol"].tolist() == ["600519", "600519"]
    assert frame["close"].tolist() == [10.0, 11.0]
    assert frame["adj_close"].tolist() == [9.0, 9.9]
    assert pd.api.types.is_datetime64_any_dtype(frame["date"])


def test_questdb_data_source_empty_result_has_schema():
    class EmptyLoader(FakeLoader):
        def ohlc(self, codes, start, end, freq="d", adjust="qfq", **kwargs):
            return {code: pd.DataFrame() for code in codes}

    source = QuestDBDataSource(EmptyLoader())
    frame = source.query(["600519"], "2025-01-01", "2025-01-05", timeframe="1d")
    assert frame.empty
    assert "adj_close" in frame.columns
    assert "symbol" in frame.columns
