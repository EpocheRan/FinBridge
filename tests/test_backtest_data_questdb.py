import pandas as pd
import pytest

from backtest.infrastructure.data.questdb import QuestDBDataSource


class FakeDB:
    def __init__(self):
        self.sql = None
        self.params = None

    def query_dataframe(self, sql, params=None):
        self.sql = sql
        self.params = params
        index = pd.DatetimeIndex(
            [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
            name="ts",
        )
        return pd.DataFrame(
            {
                "symbol": ["600519", "600519"],
                "open": [10.0, 11.0],
                "high": [10.2, 11.2],
                "low": [9.8, 10.8],
                "close": [10.0, 11.0],
                "volume": [1000.0, 2000.0],
                "amount": [10_000.0, 22_000.0],
                "adj_factor": [1.0, 1.1],
            },
            index=index,
        )


def test_questdb_data_source_builds_hfq_and_snapshot():
    db = FakeDB()
    source = QuestDBDataSource(config={"questdb": {}}, db=db)
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert "ohlcv_1d" in db.sql
    assert bundle.snapshot.rows == 2
    assert bundle.snapshot.anchor_by_symbol["600519"] == pytest.approx(1.1)
    assert bundle.frame["hfq_close"].tolist() == pytest.approx([10.0, 12.1])
    assert bundle.frame["adj_close"].iloc[-1] == pytest.approx(11.0)
    assert not bundle.frame["adjust_fallback"].any()
    assert bundle.universe.codes == ("600519",)
    assert len(bundle.calendar) == 2


def test_questdb_data_source_missing_factor_marks_fallback():
    class MissingFactorDB(FakeDB):
        def query_dataframe(self, sql, params=None):
            frame = super().query_dataframe(sql, params)
            frame["adj_factor"] = [None, None]
            return frame

    source = QuestDBDataSource(config={"questdb": {}}, db=MissingFactorDB())
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert bundle.frame["adjust_fallback"].all()
    assert bundle.frame["hfq_close"].tolist() == [10.0, 11.0]
