"""财务 Point-in-Time 与增量同步逻辑的离线测试。"""

from __future__ import annotations

from datetime import datetime

from data_interface.financial.service import FinancialDataService


def _row(period: str, publish: str, revenue: float, report_type="consolidated"):
    return {
        "period_end": datetime.strptime(period, "%Y-%m-%d"),
        "publish_date": datetime.strptime(publish, "%Y-%m-%d"),
        "report_type": report_type,
        "revenue": revenue,
    }


def test_pit_picks_version_known_at_as_of():
    rows = [
        _row("2024-09-30", "2024-10-30", 300.0),                     # 原始版本
        _row("2024-09-30", "2025-03-01", 280.0),                     # 后续重述
    ]
    visible = FinancialDataService._visible_as_of(rows, datetime(2024, 12, 31))
    assert len(visible) == 1
    assert visible[0]["revenue"] == 300.0


def test_pit_uses_restated_value_after_restatement():
    rows = [
        _row("2024-09-30", "2024-10-30", 300.0),
        _row("2024-09-30", "2025-03-01", 280.0),
    ]
    visible = FinancialDataService._visible_as_of(rows, datetime(2025, 6, 30))
    assert len(visible) == 1
    assert visible[0]["revenue"] == 280.0


def test_pit_excludes_future_reports():
    rows = [_row("2024-12-31", "2025-04-20", 440.0)]
    assert FinancialDataService._visible_as_of(rows, datetime(2025, 3, 1)) == []


def test_pit_drops_rows_without_publish_date():
    rows = [_row("2024-12-31", "2025-04-20", 440.0)]
    rows[0]["publish_date"] = None
    assert FinancialDataService._visible_as_of(rows, datetime(2025, 6, 1)) == []


def test_pit_keeps_multiple_periods_sorted():
    rows = [
        _row("2024-06-30", "2024-08-30", 220.0),
        _row("2024-12-31", "2025-04-20", 440.0),
    ]
    visible = FinancialDataService._visible_as_of(rows, datetime(2025, 6, 1))
    assert [item["period_end"].month for item in visible] == [6, 12]


def test_disclosure_lags_are_per_period_type():
    service = object.__new__(FinancialDataService)
    service.config = {"financial": {}}
    lags = service._disclosure_lags()
    # 年报披露窗口比 Q1 长得多：这是此前"2 月就认为年报缺失"的根因
    assert lags["FY"] > lags["H1"] > lags["Q1"]
    assert lags["FY"] == 120

    service.config = {"financial": {"disclosure_lag_days": {"FY": 150}}}
    assert service._disclosure_lags()["FY"] == 150


def test_expected_periods_respect_disclosure_window():
    service = object.__new__(FinancialDataService)
    service.config = {"financial": {}}
    periods = service._expected_statement_periods(2024, 2024)
    assert all(isinstance(item, datetime) for item in periods)
    # 2024 年报在 2026 年肯定已到披露期；2026 年报则不应出现
    expected_texts = {item.strftime("%Y-%m-%d") for item in periods}
    assert "2024-12-31" in expected_texts
    assert "2026-12-31" not in expected_texts
