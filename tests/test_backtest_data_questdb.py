import pandas as pd
import pytest

from backtest.infrastructure.data.questdb import QuestDBDataSource


class FakeDB:
    def __init__(self):
        self.sql = None
        self.params = None
        self.sqls: list[str] = []

    def query_dataframe(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.sqls.append(sql)
        if "stock_basic_info" in sql:
            return pd.DataFrame(
                {
                    "code": ["600519"],
                    "is_st": [False],
                    "list_date": [pd.Timestamp("2001-08-27")],
                    "delist_date": [pd.NaT],
                }
            )
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


def test_questdb_source_does_not_connect_in_constructor(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        "backtest.infrastructure.data.questdb.QuestDBClient.connect",
        lambda self: calls.append(1) or True,
    )
    source = QuestDBDataSource(config={"questdb": {}})
    assert source.db is not None
    assert calls == []


def test_questdb_data_source_builds_hfq_and_snapshot():
    db = FakeDB()
    source = QuestDBDataSource(config={"questdb": {}}, db=db)
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert any("ohlcv_1d" in sql for sql in db.sqls)
    assert bundle.snapshot.rows == 2
    assert bundle.security_status is not None
    assert bundle.security_status.list_dates["600519"] == pd.Timestamp("2001-08-27").date()
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
            if "stock_basic_info" in sql:
                return frame
            frame["adj_factor"] = [None] * len(frame)
            return frame

    source = QuestDBDataSource(config={"questdb": {}}, db=MissingFactorDB())
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert bundle.frame["adjust_fallback"].all()
    assert bundle.frame["hfq_close"].tolist() == [10.0, 11.0]
