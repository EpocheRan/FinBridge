"""AkShare 行情适配器的离线回归测试（全部用假 ak 模块，不联网）。

这些用例锁死历史上出过问题的点：

- 分钟线不能按「列数 7/9」硬套，必须按中文列名映射（且高开低收不能错位）；
- 股票周/月线必须真的请求 weekly/monthly，不能退化成日线；
- 新浪 ETF 兜底缺「成交额」时不能抛 KeyError；
- 基础信息必须返回 ``stock_basic_info`` DDL 里的列名。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from data_interface.financial.fields import PROFILE_COLUMNS
from data_interface.market.sources.akshare_source import AkshareMarketSource


def _minute_frame(columns: list[str], rows: int = 3) -> pd.DataFrame:
    base = 10.0
    values = {
        "时间": pd.date_range("2024-01-02 09:30", periods=rows, freq="min"),
        "开盘": [base] * rows,
        "收盘": [base + 0.2] * rows,
        "最高": [base + 0.3] * rows,
        "最低": [base + 0.1] * rows,
        "成交量": [10.0] * rows,
        "成交额": [1000.0] * rows,
    }
    data = {column: values.get(column, [base] * rows) for column in columns}
    return pd.DataFrame(data)


EM_MINUTE_COLUMNS = ["时间", "开盘", "收盘", "最高", "最低", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅", "换手率"]


def _source_with(fake: SimpleNamespace) -> AkshareMarketSource:
    source = AkshareMarketSource({})
    source._ak = lambda: fake  # type: ignore[assignment]
    return source


def test_stock_minute_columns_are_mapped_not_positional():
    fake = SimpleNamespace(
        stock_zh_a_hist_min_em=lambda **kwargs: _minute_frame(EM_MINUTE_COLUMNS)
    )
    frame = _source_with(fake).fetch_ohlc("600519", "2024-01-02", "2024-01-02", "5min")
    assert not frame.empty
    # 东财返回列序是 开盘/收盘/最高/最低：必须按名字映射，否则 high/low/close 会错位
    assert frame["open"].iloc[0] == pytest.approx(10.0)
    assert frame["close"].iloc[0] == pytest.approx(10.2)
    assert frame["high"].iloc[0] == pytest.approx(10.3)
    assert frame["low"].iloc[0] == pytest.approx(10.1)
    # 东财分钟成交量单位「手」-> 股
    assert frame["volume"].iloc[0] == pytest.approx(1000.0)


def test_stock_minute_handles_8_columns():
    columns = ["时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "均价"]
    fake = SimpleNamespace(stock_zh_a_hist_min_em=lambda **kwargs: _minute_frame(columns))
    frame = _source_with(fake).fetch_ohlc("600519", "2024-01-02", "2024-01-02", "1min")
    assert not frame.empty
    assert set(frame.columns) == {"code", "open", "high", "low", "close", "volume", "amount"}
    assert isinstance(frame.index, pd.DatetimeIndex)


def test_stock_weekly_requests_weekly_period():
    captured = {}

    def fake_hist(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(
            {
                "日期": pd.date_range("2024-01-05", periods=2, freq="W"),
                "开盘": [10.0, 10.5],
                "收盘": [10.4, 10.8],
                "最高": [10.6, 11.0],
                "最低": [9.9, 10.4],
                "成交量": [100.0, 120.0],
                "成交额": [10000.0, 12000.0],
                "换手率": [1.0, 1.2],
            }
        )

    fake = SimpleNamespace(stock_zh_a_hist=fake_hist)
    frame = _source_with(fake).fetch_ohlc("600519", "2024-01-01", "2024-02-01", "w")
    assert captured["period"] == "weekly"
    assert captured["adjust"] == ""  # 原始价，复权在读取端算
    assert not frame.empty
    assert frame["close"].iloc[0] == pytest.approx(10.4)


def test_stock_daily_volume_lots_to_shares():
    def fake_hist(**kwargs):
        return pd.DataFrame(
            {
                "日期": [pd.Timestamp("2024-01-02")],
                "开盘": [10.0],
                "收盘": [10.5],
                "最高": [10.6],
                "最低": [9.9],
                "成交量": [100.0],   # 手
                "成交额": [105000.0],
                "换手率": [0.8],
            }
        )

    frame = _source_with(SimpleNamespace(stock_zh_a_hist=fake_hist)).fetch_ohlc(
        "600519", "2024-01-02", "2024-01-02", "d"
    )
    assert frame["volume"].iloc[0] == pytest.approx(10000.0)
    assert frame["turnover"].iloc[0] == pytest.approx(0.8)


def test_etf_sina_fallback_missing_amount_does_not_crash():
    def fake_etf(**kwargs):
        raise RuntimeError("eastmoney down")

    sina = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-02", periods=2, freq="D"),
            "open": [1.0, 1.1],
            "high": [1.2, 1.3],
            "low": [0.9, 1.0],
            "close": [1.1, 1.2],
            "volume": [1000.0, 2000.0],
        }
    )
    fake = SimpleNamespace(
        fund_etf_hist_em=fake_etf, fund_etf_hist_sina=lambda **kwargs: sina
    )
    frame = _source_with(fake).fetch_ohlc("510300", "2024-01-01", "2024-01-05", "d")
    assert not frame.empty
    # 新浪没有成交额：用 收盘 × 成交量 估算，而不是 KeyError
    assert frame["amount"].iloc[0] == pytest.approx(1.1 * 1000.0)
    assert frame["turnover"].isna().all()


def test_stock_basic_returns_ddl_columns():
    em = pd.DataFrame(
        {
            "item": ["股票简称", "行业", "上市时间", "总股本", "流通股", "总市值", "流通市值"],
            "value": ["贵州茅台", "酿酒行业", "20010827", "1256197800", "1256197800", "2.1e12", "2.1e12"],
        }
    )
    xq = pd.DataFrame(
        {"item": ["org_name_cn", "main_operation_business"], "value": ["贵州茅台酒股份有限公司", "茅台酒生产"]}
    )
    fake = SimpleNamespace(
        stock_individual_info_em=lambda **kwargs: em,
        stock_individual_basic_info_xq=lambda **kwargs: xq,
    )
    frame = _source_with(fake).fetch_stock_basic("600519")
    assert not frame.empty
    assert set(frame.columns) <= set(PROFILE_COLUMNS)
    row = frame.iloc[0]
    assert row["code"] == "600519"
    assert row["name"] == "贵州茅台"
    assert row["industry"] == "酿酒行业"
    assert row["main_business"] == "茅台酒生产"
    assert row["exchange"] == "SSE"
    # 关键回归：绝不能出现 DDL 里不存在的列名
    assert "main_operation_biz" not in frame.columns
    assert "affiliate_industry" not in frame.columns


def test_adj_factor_parsing():
    fake_frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-02", periods=3, freq="D"),
            "hfq_factor": [1.0, 1.1, 1.2],
        }
    )
    fake = SimpleNamespace(stock_zh_a_daily=lambda **kwargs: fake_frame)
    frame = _source_with(fake).fetch_adj_factor("600519", "2024-01-01", "2024-01-05")
    assert list(frame.columns) == ["trade_date", "adj_factor"]
    assert frame["adj_factor"].iloc[-1] == pytest.approx(1.2)


def test_attach_adj_factor_maps_by_day_and_keeps_existing():
    from data_interface.market.adj_factors import attach_adj_factor

    frame = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]},
        index=pd.to_datetime(["2024-01-05", "2024-01-06", "2024-01-08"]),
    )
    factors = pd.Series({"2024-01-05": 1.1, "2024-01-08": 1.2}, dtype="float64")
    result = attach_adj_factor(frame, factors)
    assert result["adj_factor"].iloc[0] == pytest.approx(1.1)
    # 周六没有因子：沿用最近一个交易日（而不是变成 NaN）
    assert result["adj_factor"].iloc[1] == pytest.approx(1.1)
    assert result["adj_factor"].iloc[2] == pytest.approx(1.2)

    existing = frame.copy()
    existing["adj_factor"] = [9.0, 9.0, 9.0]
    assert (attach_adj_factor(existing, factors)["adj_factor"] == 9.0).all()


def test_attach_adj_factor_without_factors_sets_null():
    from data_interface.market.adj_factors import attach_adj_factor

    frame = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2024-01-05"]))
    result = attach_adj_factor(frame, None)
    assert pd.isna(result["adj_factor"].iloc[0])


def test_concept_board_ohlc_uses_named_columns():
    raw = pd.DataFrame(
        {
            "日期": pd.date_range("2024-01-02", periods=2, freq="D"),
            "开盘": [100.0, 101.0],
            "最高": [102.0, 103.0],
            "最低": [99.0, 100.0],
            "收盘": [101.0, 102.0],
            "成交量": [10.0, 11.0],
            "成交额": [1000.0, 1100.0],
        }
    )
    fake = SimpleNamespace(stock_board_concept_index_ths=lambda **kwargs: raw)
    frame = _source_with(fake).fetch_concept_board_ohlc("测试板块", "2024-01-01", "2024-01-05")
    assert list(frame.columns) == ["name", "open", "high", "low", "close", "volume", "amount"]
    assert frame["close"].iloc[0] == pytest.approx(101.0)
