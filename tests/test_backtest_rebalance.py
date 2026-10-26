import pandas as pd

from backtest.application.rebalance import RebalanceSchedule
from strategies.universe import Rebalance


def _dates() -> list[pd.Timestamp]:
    return list(pd.bdate_range("2024-01-01", "2024-01-16"))


def test_weekly_rebalance():
    dates = _dates()
    schedule = RebalanceSchedule(Rebalance.WEEKLY)
    flags = [
        schedule.is_rebalance(dt, dates[index - 1] if index else None, index)
        for index, dt in enumerate(dates)
    ]
    assert flags[0] is True
    assert flags.count(True) == 3  # 1/1, 1/8, 1/15
    assert flags[1] is False


def test_monthly_rebalance():
    dates = list(pd.bdate_range("2024-01-25", "2024-02-05"))
    schedule = RebalanceSchedule(Rebalance.MONTHLY)
    selected = schedule.dates(dates)
    assert selected[0] == pd.Timestamp("2024-01-25")
    assert selected[1] == pd.Timestamp("2024-02-01")


def test_every_n_bars():
    dates = _dates()
    schedule = RebalanceSchedule(Rebalance.EVERY_N_BARS, every_n_bars=3)
    selected = schedule.dates(dates)
    assert selected == [dates[0], dates[3], dates[6], dates[9]]
