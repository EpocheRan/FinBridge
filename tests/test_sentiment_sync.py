"""情绪数据（两融/龙虎榜/大宗）同步测试：映射、批量游标、首次续跑。"""

from __future__ import annotations

import re

import pandas as pd

from acquisition.datasets import AShareSyncService
from data_interface.sentiment import (
    SentimentService,
    map_block_trade,
    map_margin_detail,
    map_top_list,
)
from scripts.schemas.sentiment_schema import (
    BLOCK_TRADE_COLUMNS,
    MARGIN_DETAIL_COLUMNS,
    TOP_LIST_COLUMNS,
    create_sentiment_tables,
)

_COLUMN_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s+(?:STRING|SYMBOL|DOUBLE|LONG|INT|TIMESTAMP|BOOLEAN|VARCHAR)",
    re.IGNORECASE,
)


def _ddl_columns(ddl: str) -> list[str]:
    body = ddl.split(") TIMESTAMP(")[0]
    body = body[body.index("(") + 1 :]
    return [match.group(1).lower() for match in _COLUMN_RE.finditer(body)]


def _find(statements, table_name):
    return next(sql for sql in statements if f'"{table_name}"' in sql)


# ------------------------------------------------------------------ schema
def test_sentiment_ddl_matches_writer_columns():
    statements = create_sentiment_tables(None, dry_run=True)
    assert _ddl_columns(_find(statements, "margin_detail")) == MARGIN_DETAIL_COLUMNS
    assert _ddl_columns(_find(statements, "top_list")) == TOP_LIST_COLUMNS
    assert _ddl_columns(_find(statements, "block_trade")) == BLOCK_TRADE_COLUMNS


def test_block_trade_dedup_keeps_buyer():
    ddl = _find(create_sentiment_tables(None, dry_run=True), "block_trade")
    assert "DEDUP UPSERT KEYS(ts, code, buyer)" in ddl


# ------------------------------------------------------------------ mapping
def test_map_margin_detail_normalizes_code_and_numbers():
    rows = map_margin_detail(
        [{"ts_code": "600519.SH", "rzye": 1.5, "rqye": None}],
        "2026-10-29",
    )
    assert rows[0]["code"] == "600519"
    assert rows[0]["rzye"] == 1.5
    assert rows[0]["rqye"] is None
    assert rows[0]["ts"].isoformat() == "2026-10-29T00:00:00"


def test_map_top_list_renames_pct_change():
    rows = map_top_list(
        [{"ts_code": "000001.SZ", "name": "平安银行", "pct_change": 3.2, "reason": "涨幅偏离"}],
        "2026-10-29",
    )
    assert rows[0]["code"] == "000001"
    assert rows[0]["pct_chg"] == 3.2
    assert rows[0]["reason"] == "涨幅偏离"


def test_map_block_trade_keeps_buyer_seller():
    rows = map_block_trade(
        [{"ts_code": "300750.SZ", "price": 100.0, "vol": 2.0, "buyer": "机构专用"}],
        "2026-10-29",
    )
    assert rows[0]["buyer"] == "机构专用"
    assert rows[0]["seller"] is None


# ------------------------------------------------------------------ service
class FakeCalendar:
    def __init__(self, days):
        self.days = days

    def trading_days_or_weekdays(self, start, end):
        return [day for day in self.days if str(start) <= day <= str(end)]


class FakeRepo:
    def __init__(self):
        self.batches = []
        self.latest = None

    def upsert(self, dataset, rows):
        rows = list(rows)
        self.batches.append((dataset, rows))
        return len(rows)

    def latest_ts(self, dataset):
        return self.latest


class FakeTushare:
    def __init__(self, by_date):
        self.by_date = by_date

    def fetch_margin_detail(self, day):
        return self.by_date.get(day, [])

    def fetch_top_list(self, day):
        return self.by_date.get(day, [])

    def fetch_block_trade(self, day):
        return self.by_date.get(day, [])


def make_service(days, by_date):
    repo = FakeRepo()
    service = SentimentService(
        repository=repo,
        tushare=FakeTushare(by_date),
        trading_calendar=FakeCalendar(days),
        config={},
    )
    return service, repo


def test_sync_batches_by_trade_day_and_resumes_cursor():
    days = ["2026-10-28", "2026-10-29"]
    by_date = {
        "20261028": [
            {"ts_code": "600519.SH", "rzye": 1.0},
            {"ts_code": "000001.SZ", "rzye": 2.0},
        ],
        "20261029": [{"ts_code": "600519.SH", "rzye": 3.0}],
    }
    service, repo = make_service(days, by_date)

    first = service.sync("margin_detail", "2026-10-28", "2026-10-29", max_trade_days=1)
    assert first["rows_written"] == 2
    assert first["done"] is False
    assert first["next_cursor"] == "2026-10-29"
    assert [row["code"] for _, rows in repo.batches for row in rows] == ["600519", "000001"]

    second = service.sync(
        "margin_detail",
        "2026-10-28",
        "2026-10-29",
        max_trade_days=1,
        cursor=first["next_cursor"],
    )
    assert second["done"] is True
    assert second["next_cursor"] is None
    assert second["rows_written"] == 1


def test_empty_day_is_recorded_without_fake_rows():
    days = ["2026-10-29"]
    service, repo = make_service(days, {})
    result = service.sync("top_list", "2026-10-29", "2026-10-29")
    assert result["rows_written"] == 0
    assert result["empty_days"] == ["2026-10-29"]
    assert repo.batches == []


def test_unknown_dataset_rejected():
    service, _ = make_service(["2026-10-29"], {})
    try:
        service.sync("unknown", "2026-10-29", "2026-10-29")
    except ValueError as exc:
        assert "dataset" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# ------------------------------------------------------------------ wrapper
class FakeState:
    def __init__(self):
        self.saved = {}

    def get(self, task, dataset):
        return self.saved.get(dataset)

    def save(self, task, dataset, **kwargs):
        self.saved[dataset] = kwargs


class FakeSentimentService:
    def __init__(self):
        self.calls = []
        self.latest = pd.Timestamp("2026-10-28")

    def latest_ts(self, dataset):
        return self.latest

    def sync(self, dataset, start, end, **kwargs):
        self.calls.append((dataset, start, end, kwargs))
        return {
            "dataset": dataset,
            "done": True,
            "next_cursor": None,
            "rows_written": 0,
            "processed_days": [],
            "pending": [],
        }


def test_first_run_starts_from_latest_ts_and_saves_state():
    service = FakeSentimentService()
    sync_service = object.__new__(AShareSyncService)
    sync_service.settings = {}
    sync_service.state = FakeState()
    sync_service._sentiment_service = service

    result = sync_service._sync_sentiment(
        "block_trade",
        start_date="2020-01-01",
        end_date="2026-10-30",
    )

    dataset, start, end, _kwargs = service.calls[0]
    assert dataset == "block_trade"
    assert start == "2026-10-28"
    assert end == "2026-10-30"
    saved = sync_service.state.saved["block_trade"]
    assert saved["payload"]["start_date"] == "2026-10-28"
    assert result["dataset"] == "block_trade"
