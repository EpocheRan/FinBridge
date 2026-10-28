"""概念板块服务离线测试：快照、PIT、游标、部分快照保护。"""

from __future__ import annotations

import pandas as pd

from data_interface.concept.service import ConceptService


def _boards_frame():
    return pd.DataFrame(
        [
            {
                "ts": pd.Timestamp("2026-10-28"),
                "board_code": "308614",
                "name": "人工智能",
                "board_type": "concept",
                "source": "akshare.ths_index",
                "source_code": "308614",
                "status": "active",
            },
            {
                "ts": pd.Timestamp("2026-10-28"),
                "board_code": "309120",
                "name": "AI手机",
                "board_type": "concept",
                "source": "akshare.ths_index",
                "source_code": "309120",
                "status": "active",
            },
        ]
    )


class FakeBoardSource:
    source_id = "akshare.ths_index"

    def list_boards(self):
        return pd.DataFrame(
            [
                {
                    "board_code": "308614",
                    "name": "人工智能",
                    "board_type": "concept",
                    "source": self.source_id,
                    "source_code": "308614",
                },
                {
                    "board_code": "309120",
                    "name": "AI手机",
                    "board_type": "concept",
                    "source": self.source_id,
                    "source_code": "309120",
                },
            ]
        )


class FakeDailySource:
    source_id = "akshare.ths_index"

    def __init__(self):
        self.calls = []

    def fetch_board_daily(self, board, start, end):
        self.calls.append(board.board_code)
        return pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-10-27"),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "volume": 100,
                    "amount": 1000.0,
                    "pct_chg": 0.0,
                }
            ]
        )


class FakeMemberSource:
    def __init__(self, source_id="akshare.em_board", rows=2, partial=False):
        self.source_id = source_id
        self.rows = rows
        self.partial = partial
        self.calls = []

    def fetch_members(self, board):
        self.calls.append(board.board_code)
        frame = pd.DataFrame(
            {
                "stock_code": [f"60000{index}" for index in range(self.rows)],
                "stock_name": [f"S{index}" for index in range(self.rows)],
            }
        )
        frame.attrs["partial"] = self.partial
        return frame


class FakeClassSource:
    source_id = "tushare.concept"

    def fetch_classification(self):
        return pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-10-27"),
                    "concept_code": "800001",
                    "name": "传媒",
                    "class1_code": "C001",
                    "class1_name": "科技",
                    "class2_code": "C001006",
                    "class2_name": "传媒",
                    "class3_code": "C001006002",
                    "class3_name": "影视",
                    "item_size": 64,
                }
            ]
        )


class FakeRepository:
    def __init__(self, board_count=2, member_count=0):
        self.board_count = board_count
        self.member_count_value = member_count
        self.board_upserts = []
        self.daily_upserts = []
        self.member_upserts = []
        self.classification_upserts = []

    def list_boards(self, as_of=None, status="active", source=None):
        return _boards_frame().head(self.board_count)

    def find_board(self, key, as_of=None):
        frame = _boards_frame()
        matched = frame[(frame["board_code"] == key) | (frame["name"] == key)]
        return matched.iloc[0].to_dict() if not matched.empty else None

    def member_count(self, board_code, as_of=None):
        return self.member_count_value

    def members(self, board_code, as_of=None, source=None):
        return pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-10-28"),
                    "board_code": board_code,
                    "stock_code": "600000",
                    "stock_name": "浦发银行",
                    "weight": None,
                    "in_date": None,
                    "quality": "ok",
                    "source": "akshare.em_board",
                }
            ]
        )

    def upsert_boards(self, frame):
        self.board_upserts.append(frame.copy())
        return len(frame)

    def upsert_daily(self, frame):
        self.daily_upserts.append(frame.copy())
        return len(frame)

    def upsert_members(self, frame):
        self.member_upserts.append(frame.copy())
        return len(frame)

    def upsert_classification(self, frame):
        self.classification_upserts.append(frame.copy())
        return len(frame)

    def daily(self, board_codes, start, end, source=None):
        return pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp("2026-10-27"),
                    "board_code": board_codes[0],
                    "close": 1.0,
                }
            ]
        )

    def classification(self, as_of=None):
        return pd.DataFrame([{"concept_code": "800001", "name": "传媒"}])


def _service(repo, *, member_source=None, daily_source=None):
    return ConceptService(
        repository=repo,
        board_sources=[FakeBoardSource()],
        daily_source=daily_source or FakeDailySource(),
        member_sources=[member_source or FakeMemberSource()],
        classification_source=FakeClassSource(),
        config={"concept": {"member_drop_guard": 0.7}},
    )


def test_sync_boards_stamps_snapshot_and_source():
    repo = FakeRepository()
    service = _service(repo)
    result = service.sync_boards(as_of="2026-10-28")
    assert result["done"] is True
    assert result["rows_written"] == 2
    frame = repo.board_upserts[0]
    assert frame["ts"].iloc[0] == pd.Timestamp("2026-10-28")
    assert frame["status"].iloc[0] == "active"
    assert frame["source"].iloc[0] == "akshare.ths_index"


def test_sync_daily_batches_with_cursor():
    repo = FakeRepository()
    daily = FakeDailySource()
    service = _service(repo, daily_source=daily)
    result = service.sync_daily("2026-10-01", "2026-10-27", max_boards=1)
    assert result["done"] is False
    assert result["next_cursor"] == "309120"
    assert result["rows_written"] == 1
    assert daily.calls == ["308614"]

    result = service.sync_daily(
        "2026-10-01", "2026-10-27", max_boards=1, cursor=result["next_cursor"]
    )
    assert result["done"] is True
    assert daily.calls == ["308614", "309120"]


def test_sync_members_writes_snapshot_with_quality():
    repo = FakeRepository()
    service = _service(repo)
    result = service.sync_members(max_boards=1, as_of="2026-10-28")
    assert result["rows_written"] == 2
    frame = repo.member_upserts[0]
    assert frame["ts"].iloc[0] == pd.Timestamp("2026-10-28")
    assert frame["quality"].iloc[0] == "ok"
    assert frame["source"].iloc[0] == "akshare.em_board"
    assert frame["board_code"].iloc[0] == "308614"


def test_partial_snapshot_drop_is_skipped():
    repo = FakeRepository(member_count=10)
    source = FakeMemberSource(rows=3, partial=True)
    service = _service(repo, member_source=source)
    result = service.sync_members(max_boards=1, as_of="2026-10-28")
    assert result["rows_written"] == 0
    assert repo.member_upserts == []
    assert "partial snapshot" in result["errors"][0]["error"]


def test_members_resolves_name_and_sets_attrs():
    repo = FakeRepository()
    service = _service(repo)
    frame = service.members("人工智能", as_of="2026-10-28")
    assert len(frame) == 1
    assert frame.attrs["board_code"] == "308614"
    assert frame.attrs["board_name"] == "人工智能"


def test_sync_classification_stamps_source():
    repo = FakeRepository()
    service = _service(repo)
    result = service.sync_classification()
    assert result["done"] is True
    frame = repo.classification_upserts[0]
    assert frame["source"].iloc[0] == "tushare.concept"
    assert "fetched_at" in frame.columns
