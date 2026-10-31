"""PIT 股票池：快照语义、指纹、以及横截面计划的成分过滤。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.application.cross_section import plan_cross_section
from backtest.application.universe import (
    MembershipSnapshot,
    UniverseSchedule,
    explicit_schedule,
    schedule_from_snapshots,
)
from factors import F, build_panel
from strategies import CrossSectionStrategy, Rebalance, Universe

SYMBOLS = ("AAA", "BBB", "CCC", "DDD")


def test_members_at_is_point_in_time():
    schedule = schedule_from_snapshots(
        [
            ("2024-01-02", "AAA"),
            ("2024-01-02", "BBB"),
            ("2024-06-03", "BBB"),
            ("2024-06-03", "CCC"),
        ],
        label="index:test",
    )
    # 首个快照之前没有任何成分：宁可不交易，也不用未来成分
    assert schedule.members_at("2023-12-29") == frozenset()
    assert schedule.members_at("2024-03-01") == frozenset({"AAA", "BBB"})
    assert schedule.members_at("2024-06-03") == frozenset({"BBB", "CCC"})
    assert schedule.members_at("2025-01-01") == frozenset({"BBB", "CCC"})


def test_explicit_schedule_has_no_membership_filter():
    schedule = explicit_schedule(["AAA", "BBB"])
    assert not schedule.has_membership
    assert schedule.members_at("2024-01-02") == frozenset({"AAA", "BBB"})


def test_schedule_fingerprint_tracks_membership():
    base = schedule_from_snapshots([("2024-01-02", "AAA")], label="index:test")
    changed = schedule_from_snapshots(
        [("2024-01-02", "AAA"), ("2024-01-02", "BBB")], label="index:test"
    )
    assert base.fingerprint() != changed.fingerprint()
    assert base.fingerprint() == schedule_from_snapshots(
        [("2024-01-02", "AAA")], label="index:test"
    ).fingerprint()


def test_schedule_rejects_empty_codes():
    with pytest.raises(ValueError):
        UniverseSchedule(codes=())


def _frame(days: int = 40) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for index, symbol in enumerate(SYMBOLS):
        for i, dt in enumerate(dates):
            close = 10.0 + index * 5.0 + i * 0.05 * (4 - index)
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
    buffer = 0
    rebalance = Rebalance.WEEKLY

    def factors(self):
        return [F.mom(3)]

    def rank(self, panel, dt):
        return pd.Series({"AAA": 4.0, "BBB": 3.0, "CCC": 2.0, "DDD": 1.0})


def test_plan_filters_by_membership():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    panel = build_panel(frame, [F.mom(3)], start, end)

    # 第一段成分：AAA/BBB；第二段只留 DDD —— 后半段不应再选到 AAA
    mid = pd.bdate_range("2024-01-02", periods=20)[-1]
    schedule = UniverseSchedule(
        codes=SYMBOLS,
        snapshots=(
            MembershipSnapshot.build("2024-01-02", ["AAA", "BBB"]),
            MembershipSnapshot.build(mid, ["DDD"]),
        ),
        label="index:test",
    )
    plan = plan_cross_section(
        panel=panel,
        strategy=FixedRank(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=0,
        max_gross_exposure=0.9,
        rebalance="weekly",
        membership=schedule,
    )

    early = [dt for dt in plan.rebalance_dates if dt < pd.Timestamp(mid)]
    late = [dt for dt in plan.rebalance_dates if dt > pd.Timestamp(mid)]
    assert early and late
    for dt in early:
        assert set(plan.targets[dt]) == {"AAA", "BBB"}
    for dt in late:
        # 当天只有 DDD 是成分，即使 top_n=2 也只能选它
        assert set(plan.targets[dt]) == {"DDD"}
    # 后期 DDD 的 rank 是 1（成分内排名），不是全 universe 的第 4
    assert plan.rank_at(late[0], "DDD") == 1


def test_plan_without_membership_ignores_filter():
    frame = _frame()
    start = frame["date"].min().date()
    end = frame["date"].max().date()
    panel = build_panel(frame, [F.mom(3)], start, end)
    plan = plan_cross_section(
        panel=panel,
        strategy=FixedRank(),
        symbols=SYMBOLS,
        start=start,
        end=end,
        calendar=panel.dates,
        top_n=2,
        buffer=0,
        max_gross_exposure=0.9,
        rebalance="weekly",
    )
    assert all(set(target) == {"AAA", "BBB"} for target in plan.targets.values())
    assert np.isfinite(plan.score_at(plan.rebalance_dates[0], "AAA"))
