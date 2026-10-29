"""披露日历 / 公告适配器与服务离线测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.common.timeutil import now_cn
from data_interface.disclosure.service import DisclosureService, default_report_dates
from data_interface.disclosure.sources.em_source import EmDisclosureSource

CALENDAR_ROW = {
    "SECURITY_CODE": "603699",
    "SECURITY_NAME_ABBR": "纽威股份",
    "REPORT_DATE": "2026-09-30 00:00:00",
    "REPORT_TYPE_NAME": "2026年 三季报",
    "APPOINT_PUBLISH_DATE": "2026-10-30 00:00:00",
    "FIRST_APPOINT_DATE": "2026-10-20 00:00:00",
    "FIRST_CHANGE_DATE": "2026-10-25 00:00:00",
    "SECOND_CHANGE_DATE": None,
    "THIRD_CHANGE_DATE": None,
    "ACTUAL_PUBLISH_DATE": None,
    "RESIDUAL_DAYS": 2,
    "IS_PUBLISH": "0",
    "MARKET": "0101",
}

ANNOUNCEMENT_ROW = {
    "art_code": "AN202610231830839039",
    "codes": [{"stock_code": "603699", "ann_type": "A,SHA", "short_name": "纽威股份"}],
    "columns": [{"column_code": "001002006010", "column_name": "股份质押、冻结"}],
    "notice_date": "2026-10-24 00:00:00",
    "display_time": "2026-10-23 15:51:57:279",
    "title": "纽威股份:纽威股份关于控股股东部分股份质押的公告",
}


def test_standardize_calendar_maps_fields():
    frame = EmDisclosureSource._standardize_calendar([CALENDAR_ROW])
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["code"] == "603699"
    assert row["report_type"] == "2026年 三季报"
    assert str(row["appoint_date"])[:10] == "2026-10-30"
    assert row["residual_days"] == 2
    assert bool(row["is_published"]) is False


def test_standardize_calendar_marks_published():
    row = dict(CALENDAR_ROW, ACTUAL_PUBLISH_DATE="2026-10-24 00:00:00", IS_PUBLISH="1")
    frame = EmDisclosureSource._standardize_calendar([row])
    assert bool(frame.iloc[0]["is_published"]) is True


def test_standardize_calendar_drops_bad_codes():
    bad = dict(CALENDAR_ROW, SECURITY_CODE="12345")
    assert EmDisclosureSource._standardize_calendar([bad]).empty
    assert EmDisclosureSource._standardize_calendar([]).empty


def test_standardize_announcements():
    frame = EmDisclosureSource._standardize_announcements([ANNOUNCEMENT_ROW])
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["code"] == "603699"
    assert row["column_name"] == "股份质押、冻结"
    assert "质押" in row["title"]
    assert str(row["notice_date"])[:10] == "2026-10-24"


class FakeSource:
    source_id = "fake"

    def fetch_calendar(self, report_date):
        frame = pd.DataFrame([CALENDAR_ROW])
        frame["ts"] = pd.NaT
        return EmDisclosureSource._standardize_calendar([CALENDAR_ROW])

    def fetch_calendar_for_stock(self, code, limit=8):
        return EmDisclosureSource._standardize_calendar([CALENDAR_ROW])

    def fetch_announcements(self, code, **kwargs):
        return EmDisclosureSource._standardize_announcements([ANNOUNCEMENT_ROW])


class FakeRepository:
    def __init__(self):
        self.calendar_rows = None
        self.announcement_rows = None
        self._calendar = EmDisclosureSource._standardize_calendar([CALENDAR_ROW])
        # 模拟「已入库」后的可查询状态
        self._calendar["ts"] = pd.Timestamp("2026-10-28")
        self._calendar["actual_date"] = pd.NaT
        # 用相对日期，避免测试随“今天”过期
        self._calendar["appoint_date"] = pd.Timestamp(now_cn().date()) + pd.Timedelta(days=2)

    def upsert_calendar(self, frame):
        self.calendar_rows = frame
        return len(frame)

    def upsert_announcements(self, frame):
        self.announcement_rows = frame
        return len(frame)

    def calendar(self, code=None, as_of=None, report_date=None):
        return self._calendar

    def upcoming(self, days=30, as_of=None):
        return self._calendar

    def announcements(self, code=None, start=None, end=None, limit=100):
        return EmDisclosureSource._standardize_announcements([ANNOUNCEMENT_ROW])


def test_sync_calendar_writes_snapshot():
    repository = FakeRepository()
    service = DisclosureService(repository=repository, source=FakeSource(), config={})
    result = service.sync_calendar(["2026-09-30"], as_of="2026-10-28")
    assert result["rows_written"] == 1
    assert repository.calendar_rows["source"].iloc[0] == "fake"
    assert str(repository.calendar_rows["ts"].iloc[0])[:10] == "2026-10-28"


def test_sync_announcements_writes_notice_date():
    repository = FakeRepository()
    service = DisclosureService(repository=repository, source=FakeSource(), config={})
    result = service.sync_announcements(["603699"])
    assert result["rows_written"] == 1
    assert str(repository.announcement_rows["ts"].iloc[0])[:10] == "2026-10-24"


def test_upcoming_for_stock_counts_days():
    repository = FakeRepository()
    service = DisclosureService(repository=repository, source=FakeSource(), config={})
    info = service.upcoming_for_stock("603699", days=30)
    assert info is not None
    assert info["report_date"] == "2026-09-30"
    assert info["days_to_disclosure"] == 2


def test_default_report_dates_are_quarter_ends():
    dates = default_report_dates(pd.Timestamp("2026-10-28").date(), periods=4)
    assert dates[0] == "2026-09-30"
    assert "2026-06-30" in dates
    assert all(date[5:] in ("03-31", "06-30", "09-30", "12-31") for date in dates)


def test_calendar_query_stub_returns_frame():
    repository = FakeRepository()
    service = DisclosureService(repository=repository, source=FakeSource(), config={})
    frame = service.calendar("603699")
    assert not frame.empty
    assert frame.iloc[0]["code"] == "603699"
