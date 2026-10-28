"""跨模块事件 / 进度协议。"""

from __future__ import annotations

import pytest

from data_interface.common.events import DataWritten
from data_interface.common.progress import (
    NullProgressReporter,
    ProgressEvent,
)


def test_data_written_normalizes_dates_and_codes():
    event = DataWritten(
        dataset="ohlcv_1d",
        dates=("2026-10-27", "2026-10-26", "2026-10-27"),
        codes=("600519", "000001", "600519"),
        source="tushare",
        source_version="v1",
    )
    assert [item.isoformat() for item in event.dates] == ["2026-10-26", "2026-10-27"]
    assert event.codes == ("000001", "600519")
    assert event.to_payload()["dataset"] == "ohlcv_1d"


def test_data_written_rejects_empty_dataset():
    with pytest.raises(ValueError):
        DataWritten(dataset="  ")


def test_progress_event_percent():
    event = ProgressEvent(dataset="ohlcv_1d", phase="update", done=25, total=100)
    assert event.percent == 25.0
    assert ProgressEvent("x", "update", 1, 0).percent == 100.0
    assert ProgressEvent("x", "update", 200, 100).percent == 100.0


def test_null_reporter_is_noop():
    reporter = NullProgressReporter()
    reporter.start("x", 10)
    reporter.update(ProgressEvent("x", "update", 1, 10))
    reporter.finish("x", 10)
