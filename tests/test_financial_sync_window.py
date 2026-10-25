"""``sync_statements`` 取数窗口的回归测试。

历史 bug：``force=True`` 时用「当前年」做回看锚点，补历史年份（如 end_year=2024）
会算出 ``20250101~20241231`` 这样的空区间，第三方返回空表后整次同步被判定失败。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from data_interface.financial.service import FinancialDataService


class FakeTushare:
    def __init__(self):
        self.calls: list[dict] = []

    def _row(self, interface):
        return [
            {
                "ts_code": "600519.SH",
                "end_date": "20241231",
                "ann_date": "20250328",
                "report_type": "1",
                "total_revenue": 100.0,
                "revenue": 100.0,
                "oper_cost": 40.0,
                "n_income": 10.0,
            }
        ]

    def fetch_income(self, code, **kwargs):
        self.calls.append({"api": "income", **kwargs})
        return self._row("income")

    def fetch_balance_sheet(self, code, **kwargs):
        self.calls.append({"api": "balance", **kwargs})
        return [{"ts_code": "600519.SH", "end_date": "20241231", "ann_date": "20250328"}]

    def fetch_cash_flow(self, code, **kwargs):
        self.calls.append({"api": "cashflow", **kwargs})
        return [{"ts_code": "600519.SH", "end_date": "20241231", "ann_date": "20250328"}]

    def fetch_fina_indicator(self, code, **kwargs):
        self.calls.append({"api": "fina", **kwargs})
        return []


class FakeRepo:
    def __init__(self):
        self.written = []

    def get_profile(self, code):
        return {"code": code, "list_date": None, "delist_date": None}

    def get_statement_periods(self, code, statement_type, report_type="consolidated"):
        # 已有全部预期报告期 -> has_missing=False，走「强制刷新」分支
        return {
            datetime(2023, 3, 31), datetime(2023, 6, 30), datetime(2023, 9, 30),
            datetime(2023, 12, 31), datetime(2024, 3, 31), datetime(2024, 6, 30),
            datetime(2024, 9, 30), datetime(2024, 12, 31),
        }

    def get_statements(self, code, statement_type, **kwargs):
        return []

    def upsert_statements(self, statement_type, rows):
        self.written.append((statement_type, len(rows)))
        return len(rows)

    def upsert_report_meta(self, rows):
        return len(rows)

    def upsert_indicators(self, rows):
        return len(rows)

    def get_indicators(self, code, **kwargs):
        return []


def _service():
    service = object.__new__(FinancialDataService)
    service.config = {"financial": {"statement_start_year": 2000, "restatement_lookback_years": 1}}
    service._tushare = FakeTushare()
    service.repository = FakeRepo()
    service._write_raw_reports = lambda *args, **kwargs: None
    service._write_sync_state = lambda *args, **kwargs: None
    # 走「强制刷新」分支：库里已无缺失报告期
    service._missing_statement_periods = lambda *args, **kwargs: []
    service.metrics = SimpleNamespace(
        build_indicator_rows=lambda *a, **k: [],
        formula_version="v2",
    )
    return service


def test_force_refresh_window_is_anchored_on_end_year():
    service = _service()
    service.sync_statements("600519", start_year=2023, end_year=2024, force=True)

    income_call = next(call for call in service._tushare.calls if call["api"] == "income")
    assert income_call["start_date"] == "20230101"
    assert income_call["end_date"] == "20241231"


def test_force_refresh_with_current_year_keeps_lookback():
    service = _service()
    current_year = datetime.now().year
    service.sync_statements("600519", start_year=2000, end_year=current_year, force=True)

    income_call = next(call for call in service._tushare.calls if call["api"] == "income")
    assert income_call["start_date"] == f"{current_year - 1}0101"
    assert income_call["end_date"] == f"{current_year}1231"


def test_all_statements_empty_raises_instead_of_fake_success():
    service = _service()
    service._tushare = SimpleNamespace(
        fetch_income=lambda *a, **k: [],
        fetch_balance_sheet=lambda *a, **k: [],
        fetch_cash_flow=lambda *a, **k: [],
        fetch_fina_indicator=lambda *a, **k: [],
    )
    with pytest.raises(Exception) as excinfo:
        service.sync_statements("600519", start_year=2023, end_year=2024, force=True)
    assert "报表" in str(excinfo.value) or "600519" in str(excinfo.value)
