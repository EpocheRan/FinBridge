"""财务派生指标的单元测试：TTM / 单季拆分 / EV / 增长率。"""

from __future__ import annotations

from datetime import datetime

import pytest

from data_interface.financial.metrics import (
    CASH_FLOW_TTM_FIELDS,
    INCOME_TTM_FIELDS,
    FinancialMetricsCalculator,
    is_consecutive_quarters,
    quarterize,
    ttm,
)


def _row(period: str, revenue: float, report_type="consolidated", publish=None):
    period_end = datetime.strptime(period, "%Y-%m-%d")
    period_type = {3: "Q1", 6: "H1", 9: "Q3", 12: "FY"}[period_end.month]
    return {
        "period_end": period_end,
        "report_type": report_type,
        "period_type": period_type,
        "fiscal_year": period_end.year,
        "publish_date": publish,
        "revenue": revenue,
        "cost_of_revenue": revenue * 0.6,
        "gross_profit": revenue * 0.4,
        "net_income": revenue * 0.1,
    }


def _year_cumulative(year: int, base: float = 100.0):
    """构造一年四个累计报告期：100 / 220 / 330 / 440。"""
    return [
        _row(f"{year}-03-31", base),
        _row(f"{year}-06-30", base + 120),
        _row(f"{year}-09-30", base + 230),
        _row(f"{year}-12-31", base + 340),
    ]


def test_ttm_of_complete_year():
    rows = _year_cumulative(2024)
    values = ttm(rows, INCOME_TTM_FIELDS)
    assert values["revenue"] == pytest.approx(440.0)


def test_ttm_uses_four_consecutive_quarters():
    rows = _year_cumulative(2024) + [_row("2025-03-31", 120.0)]
    values = ttm(rows, INCOME_TTM_FIELDS)
    # 2024Q2..2024Q4 + 2025Q1 = 120 + 110 + 110 + 120
    assert values["revenue"] == pytest.approx(460.0)


def test_missing_quarter_returns_none_instead_of_inflated_ttm():
    """旧实现会跳过缺值行求和（实测 460 -> 560），这里必须返回 None。"""
    rows = _year_cumulative(2024) + [_row("2025-03-31", 120.0)]
    without_q3 = [row for row in rows if row["period_end"] != datetime(2024, 9, 30)]
    assert ttm(without_q3, INCOME_TTM_FIELDS)["revenue"] is None


def test_gap_in_periods_marks_partial_and_nulls():
    rows = [
        _row("2024-03-31", 100.0),   # Q1 累计 = 单季
        _row("2024-09-30", 330.0),   # 缺 H1，无法拆出 Q3
        _row("2024-12-31", 440.0),   # FY - Q3 = Q4 可以算
    ]
    quarters = quarterize(rows, ["revenue"])
    by_period = {item["period_end"].strftime("%Y-%m-%d"): item for item in quarters}
    assert by_period["2024-03-31"]["revenue"] == pytest.approx(100.0)
    assert by_period["2024-09-30"]["revenue"] is None
    assert by_period["2024-09-30"]["_single_quality"] == "GAP"
    assert by_period["2024-12-31"]["revenue"] == pytest.approx(110.0)


def test_first_row_not_q1_is_null():
    rows = [_row("2024-12-31", 440.0)]
    quarters = quarterize(rows, ["revenue"])
    assert quarters[0]["revenue"] is None
    assert quarters[0]["_single_quality"] == "MISSING_PRIOR"


def test_single_quarter_rows_are_passed_through():
    rows = [
        _row("2024-03-31", 100.0, report_type="single_quarter"),
        _row("2024-06-30", 110.0, report_type="single_quarter"),
    ]
    quarters = quarterize(rows, ["revenue"])
    assert [item["revenue"] for item in quarters] == [100.0, 110.0]
    assert ttm(rows, INCOME_TTM_FIELDS)["revenue"] is None  # 只有 2 个季度


def test_is_consecutive_quarters():
    rows = quarterize(_year_cumulative(2024), ["revenue"])
    assert is_consecutive_quarters(rows)
    assert not is_consecutive_quarters(rows[:2])


def test_valuation_has_no_ev_without_balance_sheet():
    calculator = FinancialMetricsCalculator()
    row = calculator.build_valuation_row(
        code="600519",
        trade_date=datetime(2024, 12, 31),
        daily_basic={"market_cap": 1_000_000.0, "close": 10.0},
        income_rows=_year_cumulative(2024),
        balance_rows=[],
        cash_flow_rows=[],
    )
    assert row["enterprise_value"] is None
    assert row["ev_ebitda"] is None
    assert row["ev_sales"] is None
    assert row["pe_ttm"] is not None  # PE 仍可算


def test_valuation_ev_with_balance_sheet():
    calculator = FinancialMetricsCalculator()
    balance = {
        "period_end": datetime(2024, 12, 31),
        "report_type": "consolidated",
        "total_assets": 5_000_000.0,
        "total_equity": 3_000_000.0,
        "cash_and_cash_equivalents": 200_000.0,
        "total_debt": 300_000.0,
        "total_current_assets": 1_000_000.0,
        "total_current_liabilities": 400_000.0,
    }
    row = calculator.build_valuation_row(
        code="600519",
        trade_date=datetime(2024, 12, 31),
        daily_basic={"market_cap": 1_000_000.0, "close": 10.0},
        income_rows=_year_cumulative(2024),
        balance_rows=[balance],
        cash_flow_rows=[],
    )
    assert row["enterprise_value"] == pytest.approx(1_100_000.0)


def test_yoy_uses_same_denominator_convention():
    calculator = FinancialMetricsCalculator()
    income_rows = [
        {**_row("2023-12-31", 100.0), "net_income": -100.0},
        {**_row("2024-12-31", 50.0), "net_income": 50.0},
    ]
    rows = calculator.build_indicator_rows(income_rows, [], [])
    latest = [row for row in rows if row["period_end"].year == 2024][0]
    # (50 - (-100)) / |-100| = 1.5，revenue 同口径
    assert latest["net_income_yoy"] == pytest.approx(1.5)
    assert latest["revenue_yoy"] == pytest.approx(-0.5)


def test_ttm_ignores_incomplete_fields():
    rows = _year_cumulative(2024)
    for row in rows:
        row["operating_cash_flow"] = None
    values = ttm(rows, CASH_FLOW_TTM_FIELDS)
    assert values["operating_cash_flow"] is None
