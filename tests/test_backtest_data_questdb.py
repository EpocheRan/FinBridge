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


def test_ohlc_columns_include_turnover_for_daily_tables():
    """换手类因子依赖 turnover；分钟线没有该列，不能拼进 SELECT。"""
    from backtest.infrastructure.data.questdb import _ohlc_columns

    assert "turnover" in _ohlc_columns("ohlcv_1d")
    assert "turnover" in _ohlc_columns("ohlcv_1w")
    assert "turnover" not in _ohlc_columns("ohlcv_5m")
    assert "turnover" not in _ohlc_columns("ohlcv_15m")


def test_missing_turnover_column_becomes_nan():
    # FakeDB 的帧本来就没有 turnover：loader 必须补 NaN 列而不是 KeyError
    source = QuestDBDataSource(config={"questdb": {}}, db=FakeDB())
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert "turnover" in bundle.frame.columns
    assert bundle.frame["turnover"].isna().all()


class ValuationDB(FakeDB):
    """市值表：fin_valuation_daily 的列名与面板列名不同。"""

    def query_dataframe(self, sql, params=None):
        if "fin_valuation_daily" in sql:
            index = pd.DatetimeIndex(
                [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
                name="ts",
            )
            return pd.DataFrame(
                {
                    "code": ["600519", "600519"],
                    "market_cap": [1.0e9, 1.0e9],
                    "float_market_cap": [8.0e8, 8.0e8],
                },
                index=index,
            )
        return super().query_dataframe(sql, params)


def test_valuation_extras_rename_to_size_columns():
    source = QuestDBDataSource(config={"questdb": {}}, db=ValuationDB())
    bundle = source.load(
        ["600519"],
        pd.Timestamp("2024-01-01").date(),
        pd.Timestamp("2024-01-05").date(),
        extras=("fin_valuation_daily",),
    )
    frame = bundle.frame
    assert frame["total_mv"].tolist() == [1.0e9, 1.0e9]
    assert frame["float_mv"].tolist() == [8.0e8, 8.0e8]
