"""分时结构指标测试：涨跌停价、封板/炸板、尾盘竞价、时间口径。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.market.intraday import limit_prices, session_metrics


def _bar(ts: str, price: float, volume: int = 1000):
    return {
        "ts": pd.Timestamp(ts),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
        "amount": price * volume,
    }


def test_limit_prices_main_board_and_st():
    assert limit_prices(34.91) == (38.40, 31.42)
    assert limit_prices(34.91, board="MAIN", is_st=True) == (36.66, 33.16)
    assert limit_prices(10.0, board="GEM") == (12.0, 8.0)
    # 四舍五入到分：9.99 * 1.1 = 10.989 -> 10.99
    assert limit_prices(9.99)[0] == 10.99


def test_session_metrics_detects_seal_break_and_auction():
    rows = []
    # 11:10 前逐步拉升
    for minute, price in ((9, 37.8), (10, 38.1), (11, 38.3)):
        rows.append(_bar(f"2026-10-28 11:{minute:02d}:00", price))
    # 11:12 封板，持续到 11:14
    for minute in (12, 13, 14):
        rows.append(_bar(f"2026-10-28 11:{minute:02d}:00", 38.40, volume=5000))
    # 13:32 炸板
    rows.append(_bar("2026-10-28 13:32:00", 38.31, volume=30000))
    rows.append(_bar("2026-10-28 13:33:00", 38.09, volume=30000))
    # 尾盘集合竞价并入 15:00
    rows.append(_bar("2026-10-28 15:00:00", 38.39, volume=98200))

    metrics = session_metrics(pd.DataFrame(rows), 34.91, board="MAIN")
    assert metrics is not None
    assert metrics.bars == 9
    assert metrics.limit_up == 38.40
    assert metrics.sealed_bars == 3
    assert metrics.first_seal_time == "11:12"
    assert metrics.last_seal_time == "11:14"
    assert metrics.break_count == 1
    assert metrics.auction_volume == 98200
    assert metrics.close == 38.39
    assert 0 < metrics.seal_quality <= 100
    assert metrics.max_drawdown_after_seal == pytest.approx(38.09 / 38.40 - 1, rel=1e-3)


def test_session_metrics_handles_no_seal():
    rows = [_bar("2026-10-28 09:35:00", 30.0), _bar("2026-10-28 15:00:00", 29.0)]
    metrics = session_metrics(pd.DataFrame(rows), 30.0)
    assert metrics is not None
    assert metrics.sealed_bars == 0
    assert metrics.first_seal_time is None
    assert metrics.break_count == 0


def test_session_metrics_treats_db_timestamps_as_beijing_wall_clock():
    """库内时间戳带 Z 标签但实际是北京墙钟，不能被换算成 UTC+8。"""
    rows = [
        _bar("2026-10-28 11:12:00+00:00", 38.40),
        _bar("2026-10-28 15:00:00+00:00", 38.39, volume=5000),
    ]
    metrics = session_metrics(pd.DataFrame(rows), 34.91)
    assert metrics is not None
    assert metrics.first_seal_time == "11:12"
    assert metrics.auction_volume == 5000


def test_session_metrics_insufficient_data():
    assert session_metrics(pd.DataFrame(), 10.0) is None
    one = pd.DataFrame([_bar("2026-10-28 09:35:00", 10.0)])
    assert session_metrics(one, 10.0) is None


def test_close_position_and_range():
    rows = [
        _bar("2026-10-28 09:35:00", 34.5),
        _bar("2026-10-28 10:00:00", 38.4),
        _bar("2026-10-28 15:00:00", 38.39, volume=100),
    ]
    metrics = session_metrics(pd.DataFrame(rows), 34.91)
    assert metrics is not None
    assert metrics.close_position == pytest.approx((38.39 - 34.5) / (38.4 - 34.5), rel=1e-3)
    assert metrics.range_pct == pytest.approx((38.4 - 34.5) / 34.91, rel=1e-3)
