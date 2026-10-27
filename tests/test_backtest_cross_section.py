"""横截面预选计划：全 universe 排名 → 目标权重 + 候选集。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.application.cross_section import plan_cross_section, should_keep_buffer
from factors import F, build_panel
from strategies import CrossSectionStrategy, Rebalance, Universe

SYMBOLS = ("AAA", "BBB", "CCC", "DDD")


def _frame(days: int = 60) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for index, symbol in enumerate(SYMBOLS):
        for i, dt in enumerate(dates):
            close = 10.0 + index * 5.0 + i * 0.05 * (4 - index) + np.sin(i / 5 + index)
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 10_000,
                    "adj_factor": 1.0,
                    "hfq_close": close,
                }
            )
    return pd.DataFrame(rows)


class FixedRank(CrossSectionStrategy):
    name = "fixed"
    universe = Universe.explicit(SYMBOLS)
    top_n = 2
    buffer = 1
    rebalance = Rebalance.WEEKLY

    def factors(self):
        return [F.mom(5)]

    def rank(self, panel, dt):
        return pd.Series({"AAA": 4.0, "BBB": 3.0, "CCC": 2.0, "DDD": 1.0})


def _panel(frame: pd.DataFrame):
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    return build_panel(frame, [F.mom(5)], start, end), start, end


def test_plan_candidates_targets_and_selections():
    frame = _frame()
    panel, start, end = _panel(frame)
    strategy = FixedRank()
    plan = plan_cross_section(
        panel=panel,
        strategy=strategy,
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=1,
        max_gross_exposure=0.9,
        rebalance="weekly",
    )

    assert plan.universe_size == len(SYMBOLS)
    assert plan.candidates == ("AAA", "BBB")
    # 混合架构的候选集应小于 universe（固定排名时只选 2 只）
    assert len(plan.candidates) < plan.universe_size
    assert len(plan.rebalance_dates) > 5
    for target in plan.targets.values():
        assert set(target) == {"AAA", "BBB"}
        assert sum(target.values()) == pytest.approx(0.9, abs=1e-9)

    # 每个调仓日选 2 只，权重相等
    grouped = plan.selections.groupby("date")
    assert (grouped.size() == 2).all()
    assert (grouped["weight"].sum() <= 0.9 + 1e-9).all()

    first = plan.rebalance_dates[0]
    assert plan.rank_at(first, "AAA") == 1
    assert plan.rank_at(first, "BBB") == 2
    # ranks 只保留候选集（buffer 退出只需要持仓/候选的 rank）
    assert plan.rank_at(first, "CCC") is None
    assert plan.score_at(first, "AAA") == 4.0


def test_plan_honours_buffer_and_ranks_all_dates():
    frame = _frame()
    panel, start, end = _panel(frame)
    plan = plan_cross_section(
        panel=panel,
        strategy=FixedRank(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=2,
        max_gross_exposure=0.9,
        rebalance="weekly",
    )
    # 每日 rank 都要有，供非调仓日 buffer 退出使用
    assert plan.ranks.index.min() == pd.Timestamp(start)
    assert plan.ranks.index.max() == pd.Timestamp(end)
    assert plan.rank_at(end, "BBB") == 2


def test_should_keep_buffer():
    assert should_keep_buffer(1, top_n=2, buffer=1) is True
    assert should_keep_buffer(3, top_n=2, buffer=1) is True
    assert should_keep_buffer(4, top_n=2, buffer=1) is False
    assert should_keep_buffer(None, top_n=2, buffer=1) is False


def test_plan_every_n_bars_uses_configured_interval():
    frame = _frame()
    panel, start, end = _panel(frame)
    plan = plan_cross_section(
        panel=panel,
        strategy=FixedRank(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=1,
        max_gross_exposure=0.9,
        rebalance="every_n_bars",
        every_n_bars=5,
    )
    indexes = [panel.dates.get_loc(dt) for dt in plan.rebalance_dates]
    assert indexes[:3] == [0, 5, 10]


def test_plan_drops_custom_weights_for_non_selected_symbols():
    frame = _frame()
    panel, start, end = _panel(frame)

    class Extra(FixedRank):
        def weights(self, panel, scores, selected):
            return {**{symbol: 0.4 for symbol in selected}, "DDD": 5.0}

    plan = plan_cross_section(
        panel=panel,
        strategy=Extra(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=1,
        max_gross_exposure=0.9,
        rebalance="weekly",
    )
    for target in plan.targets.values():
        assert set(target) == {"AAA", "BBB"}


def test_plan_normalizes_custom_weights_to_gross_exposure():
    frame = _frame()
    panel, start, end = _panel(frame)

    class Heavy(FixedRank):
        def weights(self, panel, scores, selected):
            return {symbol: 0.8 for symbol in selected}

    plan = plan_cross_section(
        panel=panel,
        strategy=Heavy(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=0,
        max_gross_exposure=0.6,
        rebalance="weekly",
    )
    for target in plan.targets.values():
        assert sum(target.values()) == pytest.approx(0.6, abs=1e-9)
