"""分档服务离线测试：批量游标、质量标记、源回退、源元数据。"""

from __future__ import annotations

import pandas as pd

from data_interface.moneyflow.models import CANONICAL_COLUMNS, TUSHARE_META
from data_interface.moneyflow.service import MoneyflowService


def _canonical_frame(rows: int = 3, source: str = "tushare.moneyflow") -> pd.DataFrame:
    records = []
    for index in range(rows):
        record = {column: None for column in CANONICAL_COLUMNS}
        record.update(
            {
                "ts": pd.Timestamp("2026-10-27"),
                "code": f"60000{index}",
                "market": "MLCN",
                "buy_sm_vol": 100,
                "buy_sm_amount": 1000.0,
                "sell_sm_vol": 100,
                "sell_sm_amount": 900.0,
                "buy_md_vol": 100,
                "buy_md_amount": 1000.0,
                "sell_md_vol": 100,
                "sell_md_amount": 900.0,
                "buy_lg_vol": 100,
                "buy_lg_amount": 1000.0,
                "sell_lg_vol": 100,
                "sell_lg_amount": 900.0,
                "buy_elg_vol": 100,
                "buy_elg_amount": 1000.0,
                "sell_elg_vol": 100,
                "sell_elg_amount": 900.0,
                "net_mf_vol": 0,
                "net_mf_amount": 100.0,
                "source": source,
                "source_version": "v1",
                "quality": "ok",
                "quality_note": None,
                "fetched_at": pd.Timestamp("2026-10-27 16:00:00"),
            }
        )
        records.append(record)
    return pd.DataFrame(records, columns=CANONICAL_COLUMNS)


class FakeCalendar:
    def __init__(self, days):
        self.days = days

    def trading_days_or_weekdays(self, start, end):
        return [day for day in self.days if start <= day <= end]


class FakeRepository:
    def __init__(self):
        self.upserts = []
        self.meta = []
        self.queries = []

    def upsert(self, frame):
        self.upserts.append(frame.copy())
        return len(frame)

    def upsert_source_meta(self, payload):
        self.meta.append(payload)
        return 1

    def query_by_code(self, code, start, end, source=None):
        self.queries.append(("code", code, start, end, source))
        return pd.DataFrame()

    def query_by_date(self, trade_date, source=None):
        self.queries.append(("date", trade_date, source))
        return pd.DataFrame()

    def rank(self, trade_date, field, *, source, limit=50, ascending=False):
        self.queries.append(("rank", trade_date, field, source, limit))
        return pd.DataFrame()

    def latest_ts(self, source=None):
        return pd.Timestamp("2026-10-27")

    def sources(self, only_enabled=False):
        return pd.DataFrame()

    def resolve_source(self, preferred=None):
        return "tushare.moneyflow"


class FakeSource:
    def __init__(self, source_id="tushare.moneyflow", rows=3, fail=False):
        self.source_id = source_id
        self.rows = rows
        self.fail = fail
        self.calls = []
        self.meta = TUSHARE_META

    def fetch_by_date(self, day):
        self.calls.append(day)
        if self.fail:
            raise RuntimeError("boom")
        frame = _canonical_frame(self.rows, source=self.source_id)
        frame["ts"] = pd.Timestamp(day)
        return frame


def _service(repo, sources, days, min_rows=2):
    return MoneyflowService(
        repository=repo,
        sources=sources,
        config={"moneyflow": {"quality_min_rows": min_rows}},
        trading_calendar=FakeCalendar(days),
    )


def test_sync_batches_and_cursor():
    repo = FakeRepository()
    source = FakeSource()
    service = _service(repo, [source], ["2026-10-26", "2026-10-27", "2026-10-28"])

    result = service.sync("2026-10-26", "2026-10-28", max_trade_days=2)

    assert result["done"] is False
    assert result["next_cursor"] == "2026-10-28"
    assert result["rows_written"] == 6
    assert result["processed_days"] == ["2026-10-26", "2026-10-27"]
    assert len(repo.upserts) == 2

    result = service.sync("2026-10-26", "2026-10-28", max_trade_days=2, cursor=result["next_cursor"])
    assert result["done"] is True
    assert result["next_cursor"] is None
    assert result["processed_days"] == ["2026-10-28"]


def test_quality_marks_partial_for_small_batch():
    repo = FakeRepository()
    source = FakeSource(rows=1)
    service = _service(repo, [source], ["2026-10-27"], min_rows=2)
    service.sync("2026-10-27", "2026-10-27", max_trade_days=1)
    assert repo.upserts[0]["quality"].iloc[0] == "partial"
    assert "min_rows" in str(repo.upserts[0]["quality_note"].iloc[0])


def test_falls_back_to_next_source():
    repo = FakeRepository()
    first = FakeSource(source_id="tushare.moneyflow", fail=True)
    second = FakeSource(source_id="akshare.em")
    service = _service(repo, [first, second], ["2026-10-27"])
    result = service.sync("2026-10-27", "2026-10-27", max_trade_days=1)
    assert result["rows_written"] == 3
    assert result["source_rows"] == {"akshare.em": 3}
    assert first.calls == ["2026-10-27"]
    assert second.calls == ["2026-10-27"]


def test_empty_day_recorded_as_error():
    repo = FakeRepository()
    source = FakeSource(rows=0)
    service = _service(repo, [source], ["2026-10-27"])
    result = service.sync("2026-10-27", "2026-10-27", max_trade_days=1)
    assert result["rows_written"] == 0
    assert result["errors"][0]["date"] == "2026-10-27"


def test_ensure_source_meta_writes_definition():
    repo = FakeRepository()
    service = _service(repo, [FakeSource()], ["2026-10-27"])
    written = service.ensure_source_meta()
    assert written == 1
    assert repo.meta[0]["source"] == "tushare.moneyflow"
    assert "万元" in repo.meta[0]["definition"]


def test_get_by_code_uses_resolved_source():
    repo = FakeRepository()
    service = _service(repo, [FakeSource()], ["2026-10-27"])
    service.get_by_code("600519", "2026-10-01", "2026-10-27")
    assert repo.queries[0][0] == "code"
    assert repo.queries[0][-1] == "tushare.moneyflow"


def test_empty_day_is_retried_via_pending():
    class FlakySource(FakeSource):
        def __init__(self):
            super().__init__()
            self.failed = set()

        def fetch_by_date(self, day):
            if day == "2026-10-26" and day not in self.failed:
                self.failed.add(day)
                self.calls.append(day)
                return pd.DataFrame(columns=CANONICAL_COLUMNS)
            return super().fetch_by_date(day)

    repo = FakeRepository()
    source = FlakySource()
    service = _service(repo, [source], ["2026-10-26", "2026-10-27"])

    first = service.sync("2026-10-26", "2026-10-27", max_trade_days=2)
    assert first["pending"] == ["2026-10-26"]
    assert first["rows_written"] == 3

    second = service.sync(
        "2026-10-26", "2026-10-27", max_trade_days=1, pending=first["pending"]
    )
    assert second["pending"] == []
    assert second["processed_days"] == ["2026-10-26"]
    assert second["rows_written"] == 3
