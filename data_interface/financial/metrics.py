"""财务派生指标：TTM、EV、估值倍数、成长/盈利能力。

报表原始数据可能是累计值（Q1/H1/Q3/FY），这里统一拆成单季度后再滚动
求和，避免直接用累计值做 TTM 导致重复计算。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

INCOME_TTM_FIELDS = [
    "revenue",
    "total_revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_and_development_expense",
    "selling_expense",
    "general_and_admin_expense",
    "selling_general_admin_expense",
    "finance_expense",
    "interest_expense",
    "depreciation_and_amortization",
    "operating_profit",
    "ebit",
    "ebitda",
    "pretax_profit",
    "income_tax",
    "net_income",
    "net_income_attributable_to_parent",
    "minority_interest",
    "continuing_operations_net_income",
    "discontinued_operations_net_income",
]

CASH_FLOW_TTM_FIELDS = [
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    "depreciation_and_amortization",
    "change_in_working_capital",
    "dividends_paid",
]


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return numerator / denominator
    except ZeroDivisionError:
        return None


def _sub(a: float | None, b: float | None) -> float | None:
    if a is None:
        return None
    return a - (b or 0.0)


def quarterize(rows: Iterable[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    """把累计报表拆成单季度。

    - ``report_type == single_quarter`` 直接使用；
    - 否则按 fiscal_year 内 Q1 -> H1 -> Q3 -> FY 逐级相减。
    """
    deduped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["period_end"], row.get("report_type"))
        deduped.setdefault(key, row)
    ordered = sorted(deduped.values(), key=lambda item: item["period_end"])

    # 同一报告期可能同时存在合并/调整/母公司版本；优先使用合并报表。
    consolidated = [
        row for row in ordered if row.get("report_type") == "consolidated"
    ]
    if consolidated:
        ordered = consolidated
    else:
        ordered = [
            row
            for row in ordered
            if not str(row.get("report_type", "")).startswith("parent")
        ]

    by_year: dict[int, list[dict[str, Any]]] = {}
    single_rows: list[dict[str, Any]] = []

    for row in ordered:
        if row.get("report_type") == "single_quarter":
            single_rows.append({**row, "_single": True})
        else:
            by_year.setdefault(row.get("fiscal_year") or row["period_end"].year, []).append(row)

    result: list[dict[str, Any]] = list(single_rows)
    for year_rows in by_year.values():
        previous: dict[str, Any] | None = None
        for row in year_rows:
            item = dict(row)
            if previous is None and row.get("period_type") == "Q1":
                pass
            elif previous is not None:
                for field in fields:
                    item[field] = _sub(row.get(field), previous.get(field))
            else:
                # 数据从 H1/Q3/FY 开始，无法拆出 Q1，保留 None
                for field in fields:
                    item[field] = None
            item["_single"] = True
            result.append(item)
            previous = row
    return sorted(result, key=lambda item: item["period_end"])


def ttm(rows: Iterable[dict[str, Any]], fields: list[str]) -> dict[str, float | None]:
    quarters = quarterize(rows, fields)
    recent = quarters[-4:]
    if not recent:
        return {field: None for field in fields}
    result: dict[str, float | None] = {}
    for field in fields:
        values = [row.get(field) for row in recent if row.get(field) is not None]
        result[field] = sum(values) if values else None
    return result


def latest_row(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: (item["period_end"], item.get("publish_date") or datetime.min))
    return ordered[-1] if ordered else {}


class FinancialMetricsCalculator:
    formula_version = "v1"

    def build_valuation_row(
        self,
        *,
        code: str,
        trade_date: datetime,
        daily_basic: dict[str, Any],
        income_rows: list[dict[str, Any]],
        balance_rows: list[dict[str, Any]],
        cash_flow_rows: list[dict[str, Any]],
        previous_market_cap: float | None = None,
    ) -> dict[str, Any]:
        income_ttm = ttm(income_rows, INCOME_TTM_FIELDS)
        cash_ttm = ttm(cash_flow_rows, CASH_FLOW_TTM_FIELDS)
        balance = latest_row(balance_rows)

        market_cap = daily_basic.get("market_cap")
        close = daily_basic.get("close")
        total_share = daily_basic.get("total_share")
        if market_cap is None and close is not None and total_share is not None:
            market_cap = close * total_share

        cash = balance.get("cash_and_cash_equivalents")
        total_debt = balance.get("total_debt")
        if total_debt is None:
            total_debt = (balance.get("short_term_debt") or 0.0) + (
                balance.get("long_term_debt") or 0.0
            )
        enterprise_value = None
        if market_cap is not None:
            enterprise_value = market_cap + (total_debt or 0.0) - (cash or 0.0)

        net_income_ttm = income_ttm.get("net_income")
        revenue_ttm = income_ttm.get("revenue") or income_ttm.get("total_revenue")
        gross_profit_ttm = income_ttm.get("gross_profit")
        ebit_ttm = income_ttm.get("ebit")
        ebitda_ttm = income_ttm.get("ebitda")
        ocf_ttm = cash_ttm.get("operating_cash_flow")
        fcf_ttm = cash_ttm.get("free_cash_flow")
        equity = balance.get("total_equity")
        current_assets = balance.get("total_current_assets")
        current_liabilities = balance.get("total_current_liabilities")
        net_working_capital = None
        if current_assets is not None and current_liabilities is not None:
            net_working_capital = current_assets - current_liabilities

        market_cap_change_pct = None
        if previous_market_cap not in (None, 0) and market_cap is not None:
            market_cap_change_pct = market_cap / previous_market_cap - 1

        return {
            "ts": trade_date,
            "code": code,
            "close": close,
            "total_share": total_share,
            "market_cap": market_cap,
            "float_market_cap": daily_basic.get("float_market_cap"),
            "enterprise_value": enterprise_value,
            "pe_ttm": daily_basic.get("pe_ttm") or _safe_div(market_cap, net_income_ttm),
            "pb_mrq": daily_basic.get("pb_mrq") or _safe_div(market_cap, equity),
            "ps_ttm": daily_basic.get("ps_ttm") or _safe_div(market_cap, revenue_ttm),
            "pcf_ttm": _safe_div(market_cap, ocf_ttm),
            "pfcf": _safe_div(market_cap, fcf_ttm),
            "ev_ebitda": _safe_div(enterprise_value, ebitda_ttm),
            "ev_ebit": _safe_div(enterprise_value, ebit_ttm),
            "ev_sales": _safe_div(enterprise_value, revenue_ttm),
            "ev_gross_profit": _safe_div(enterprise_value, gross_profit_ttm),
            "ev_fcf": _safe_div(enterprise_value, fcf_ttm),
            "p_nwc": _safe_div(market_cap, net_working_capital),
            "earnings_yield": _safe_div(net_income_ttm, market_cap),
            "dividend_yield": daily_basic.get("dividend_yield"),
            "market_cap_change_pct": market_cap_change_pct,
            "source": daily_basic.get("source", "derived"),
            "formula_version": self.formula_version,
            "ingested_at": daily_basic.get("ingested_at") or datetime.utcnow(),
        }

    def build_indicator_rows(
        self,
        income_rows: list[dict[str, Any]],
        balance_rows: list[dict[str, Any]],
        cash_flow_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按报告期输出常用财务指标，供 MCP 查询。"""
        income_by_period = {row["period_end"]: row for row in income_rows}
        balance_by_period = {row["period_end"]: row for row in balance_rows}
        cash_by_period = {row["period_end"]: row for row in cash_flow_rows}
        periods = sorted(set(income_by_period) | set(balance_by_period))

        # 同比按“相同报告期类型、上一年度”比较，避免 Q1 与 FY 直接相减
        history = {
            (row.get("period_type"), row.get("fiscal_year")): row
            for row in income_rows
        }

        result = []
        for period in periods:
            income = income_by_period.get(period, {})
            balance = balance_by_period.get(period, {})
            cash = cash_by_period.get(period, {})
            revenue = income.get("revenue") or income.get("total_revenue")
            net_income = income.get("net_income")
            equity = balance.get("total_equity")
            assets = balance.get("total_assets")
            previous = history.get(
                (income.get("period_type"), (income.get("fiscal_year") or 0) - 1),
                {},
            )
            previous_revenue = previous.get("revenue") or previous.get("total_revenue")
            previous_profit = previous.get("net_income")
            result.append(
                {
                    "ts": period,
                    "period_end": period,
                    "report_type": income.get("report_type"),
                    "revenue": revenue,
                    "net_income": net_income,
                    "gross_margin": _safe_div(income.get("gross_profit"), revenue),
                    "net_margin": _safe_div(net_income, revenue),
                    "roe": _safe_div(net_income, equity),
                    "roa": _safe_div(net_income, assets),
                    "debt_to_assets": _safe_div(balance.get("total_liabilities"), assets),
                    "current_ratio": _safe_div(
                        balance.get("total_current_assets"),
                        balance.get("total_current_liabilities"),
                    ),
                    "ocf": cash.get("operating_cash_flow"),
                    "free_cash_flow": cash.get("free_cash_flow"),
                    "revenue_yoy": _safe_div(
                        revenue - previous_revenue, previous_revenue
                    )
                    if previous_revenue not in (None, 0) and revenue is not None
                    else None,
                    "net_income_yoy": _safe_div(
                        net_income - previous_profit, abs(previous_profit)
                    )
                    if previous_profit not in (None, 0) and net_income is not None
                    else None,
                }
            )
        return result
