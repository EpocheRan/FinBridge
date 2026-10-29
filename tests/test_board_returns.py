"""板块收益自算测试（不依赖数据库：全部用假的 repository）。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.concept.service import ConceptService


class FakeRepository:
    def __init__(self, members, prices, caps, names):
        self._members = members
        self._prices = prices
        self._caps = caps
        self._names = names
        self.written = None

    def board_members(self, board_id, as_of=None):
        return self._members.get(board_id, [])

    def daily_prices(self, codes, start, end, chunk=200):
        return self._prices[self._prices["code"].isin(codes)].copy()

    def float_market_caps(self, codes, start, end, chunk=200):
        return self._caps[self._caps["code"].isin(codes)].copy()

    def dim_boards(self, board_type=None):
        return pd.DataFrame(
            [{"board_id": key, "board_name": value} for key, value in self._names.items()]
        )

    def upsert_daily(self, frame):
        self.written = frame
        return len(frame)


def _prices():
    dates = pd.to_datetime(["2026-10-27", "2026-10-28"])
    rows = []
    # A: 10 -> 11 (+10%)，B: 10 -> 10 (0%)
    for code, closes in (("A", (10.0, 11.0)), ("B", (10.0, 10.0))):
        for ts, close in zip(dates, closes):
            rows.append({"ts": ts, "code": code, "close": close})
    return pd.DataFrame(rows)


def _caps():
    dates = pd.to_datetime(["2026-10-27", "2026-10-28"])
    rows = []
    for code, cap in (("A", 1.0), ("B", 3.0)):
        for ts in dates:
            rows.append({"ts": ts, "code": code, "float_market_cap": cap})
    return pd.DataFrame(rows)


def _service(weight_caps=None):
    repository = FakeRepository(
        members={"BK0545": ["A", "B"]},
        prices=_prices(),
        caps=_caps() if weight_caps is None else weight_caps,
        names={"BK0545": "通用设备"},
    )
    return ConceptService(repository=repository, config={}), repository


def test_equal_weight_board_return():
    service, repository = _service(weight_caps=pd.DataFrame(columns=["ts", "code", "float_market_cap"]))
    summary = service.compute_board_daily(["BK0545"], "2026-10-27", "2026-10-28", weight="equal")
    # 首日没有前收盘，自动跳过，只留 2026-10-28 一天
    assert summary["written"] == 1
    frame = repository.written.sort_values("ts")
    assert frame["pct_chg"].iloc[-1] == pytest.approx(5.0, abs=1e-6)
    assert frame["board_code"].unique().tolist() == ["BK0545"]
    assert frame["source"].unique().tolist() == ["computed.bk"]
    assert frame["close"].iloc[-1] == pytest.approx(1000 * 1.05, rel=1e-6)


def test_float_mv_weighted_board_return():
    service, repository = _service()
    summary = service.compute_board_daily(["BK0545"], "2026-10-27", "2026-10-28", weight="float_mv")
    frame = repository.written.sort_values("ts")
    # 权重 A:B = 1:3 → (1*10% + 3*0%) / 4 = 2.5%
    assert frame["pct_chg"].iloc[-1] == pytest.approx(2.5, abs=1e-6)
    assert summary["members"] == 2


def test_empty_inputs_are_safe():
    service, _ = _service()
    assert service.compute_board_daily([], "2026-10-27", "2026-10-28")["rows"] == 0

    repository = FakeRepository(members={}, prices=_prices(), caps=_caps(), names={})
    svc = ConceptService(repository=repository, config={})
    assert svc.compute_board_daily(["BK9999"], "2026-10-27", "2026-10-28")["rows"] == 0


def test_window_filters_older_rows():
    service, repository = _service()
    service.compute_board_daily(["BK0545"], "2026-10-28", "2026-10-28", weight="equal")
    frame = repository.written
    assert len(frame) == 1
    assert str(frame["ts"].iloc[0])[:10] == "2026-10-28"
