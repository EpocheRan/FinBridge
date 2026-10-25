"""财务派生指标：TTM、EV、估值倍数、成长/盈利能力。

关键不变量（2026-10 修订）：

1. 累计报表 -> 单季度：只允许 **相邻报告期** 相减（Q1→H1→Q3→FY）。
   跳期（缺 H1 直接拿到 Q3）算不出单季，一律置 ``None`` 并标 ``PARTIAL``，
   不再用「当前累计值 - 上一期累计值」硬凑。
2. TTM：必须是**连续 4 个季度**且 4 个值都非空才求和；缺一期就返回 ``None``，
   PE/PS/EV 也跟着为 ``None``，绝不输出「看起来正常」的错数。
3. EV：资产负债表缺失时 ``enterprise_value`` 为 ``None``（不能退化成市值），
   对应 ``ev_*`` 全部为 ``None``。
4. 增长率：revenue_yoy 与 net_income_yoy 统一用 ``(本期-上期)/|上期|``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from data_interface.common.timeutil import now_cn

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

#: 报告期类型 -> 累计进度序号（用于相邻性判断）
PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}
#: 年末月份 -> 季度序号（3/6/9/12）
MONTH_TO_QUARTER = {3: 0, 6: 1, 9: 2, 12: 3}

QUALITY_OK = "OK"
QUALITY_SINGLE = "SINGLE_QUARTER"
QUALITY_MISSING_PRIOR = "MISSING_PRIOR"
QUALITY_GAP = "GAP"
QUALITY_ANOMALY = "ANOMALY"


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return numerator / denominator
    except ZeroDivisionError:
        return None


def _sub(a: float | None, b: float | None) -> float | None:
    """累计值相减；任一缺失返回 None（绝不把缺失前值当 0）。"""
    if a is None or b is None:
        return None
    return a - b


def _yoy(current: float | None, previous: float | None) -> float | None:
    """统一增长率口径：``(本期-上期)/|上期|``。"""
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous)


def quarter_index(period_end: datetime) -> int | None:
    """报告期 -> 连续季度序号（年*4 + 季），用于相邻性判断。"""
    quarter = MONTH_TO_QUARTER.get(period_end.month)
    if quarter is None:
        return None
    return period_end.year * 4 + quarter


def quarterize(rows: Iterable[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    """把累计报表拆成单季度。

    - ``report_type == single_quarter`` 直接使用（标记 ``QUALITY_SINGLE``）；
    - 其余按 fiscal_year 内 Q1 → H1 → Q3 → FY 逐级相减；
    - 跳期/重复/缺前值 的条目对应字段置 ``None``，并写 ``_single_quality``。
    """
    deduped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["period_end"], row.get("report_type"))
        deduped.setdefault(key, row)
    ordered = sorted(deduped.values(), key=lambda item: item["period_end"])

    # 同一报告期可能同时存在合并/调整/母公司版本；优先使用合并报表。
    consolidated = [row for row in ordered if row.get("report_type") == "consolidated"]
    if consolidated:
        ordered = consolidated
    else:
        ordered = [
            row
            for row in ordered
            if not str(row.get("report_type", "")).startswith("parent")
        ]

    by_year: dict[int, list[dict[str, Any]]] = {}
    result: list[dict[str, Any]] = []

    for row in ordered:
        if row.get("report_type") == "single_quarter":
            result.append({**row, "_single": True, "_single_quality": QUALITY_SINGLE})
        else:
            by_year.setdefault(row.get("fiscal_year") or row["period_end"].year, []).append(row)

    for year_rows in by_year.values():
        year_rows.sort(key=lambda item: item["period_end"])
        previous: dict[str, Any] | None = None
        previous_order: int | None = None
        for row in year_rows:
            item = dict(row)
            order = PERIOD_ORDER.get(str(row.get("period_type")), None)
            quality = QUALITY_OK

            if order is None:
                quality = QUALITY_ANOMALY
            elif previous is None:
                if order == PERIOD_ORDER["Q1"]:
                    # Q1 累计值本身就是单季值
                    quality = QUALITY_OK
                else:
                    for field in fields:
                        item[field] = None
                    quality = QUALITY_MISSING_PRIOR
            elif previous_order is not None and order - previous_order == 1:
                for field in fields:
                    item[field] = _sub(row.get(field), previous.get(field))
                if any(item.get(field) is None for field in fields):
                    quality = QUALITY_OK  # 单个字段缺失是源数据问题，不否整行
            else:
                for field in fields:
                    item[field] = None
                quality = QUALITY_GAP

            item["_single"] = True
            item["_single_quality"] = quality
            result.append(item)
            previous = row
            previous_order = order

    return sorted(result, key=lambda item: item["period_end"])


def is_consecutive_quarters(rows: list[dict[str, Any]]) -> bool:
    """四行是否严格连续四个季度（3 月/6 月/9 月/12 月各一次）。"""
    if len(rows) != 4:
        return False
    indexes = []
    for row in rows:
        index = quarter_index(row["period_end"])
        if index is None:
            return False
        indexes.append(index)
    return indexes == list(range(indexes[0], indexes[0] + 4))


def ttm(
    rows: Iterable[dict[str, Any]],
    fields: list[str],
    *,
    require_complete: bool = True,
) -> dict[str, float | None]:
    """滚动 12 个月合计。

    只有「连续 4 个季度 + 该字段 4 期皆非空」才返回数值，否则返回 ``None``。
    这样 PE/PS/EV 在缺季时会显示为空，避免输出虚高的 TTM。
    """
    quarters = [row for row in quarterize(rows, fields) if row.get("_single")]
    quarters.sort(key=lambda item: item["period_end"])
    recent = quarters[-4:]
    if len(recent) < 4 or (require_complete and not is_consecutive_quarters(recent)):
        return {field: None for field in fields}

    result: dict[str, float | None] = {}
    for field in fields:
        values = [row.get(field) for row in recent]
        if any(value is None for value in values):
            result[field] = None
        else:
            result[field] = sum(values)
    return result


def latest_row(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda item: (
            item["period_end"],
            item.get("publish_date") or datetime.min,
        ),
    )
    return ordered[-1] if ordered else {}


class FinancialMetricsCalculator:
    formula_version = "v2"

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
        # 没有资产负债表就不能假装 EV = 市值
        balance_available = bool(balance) and any(
            balance.get(key) is not None
            for key in (
                "total_assets",
                "total_equity",
                "total_liabilities",
                "cash_and_cash_equivalents",
                "total_debt",
            )
        )
        enterprise_value = None
        if market_cap is not None and balance_available:
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
            "ingested_at": daily_basic.get("ingested_at") or now_cn(),
        }

    def build_indicator_rows(
        self,
        income_rows: list[dict[str, Any]],
        balance_rows: list[dict[str, Any]],
        cash_flow_rows: list[dict[str, Any]],
        *,
        code: str | None = None,
    ) -> list[dict[str, Any]]:
        """按报告期输出常用财务指标。

        入参必须是**全量历史**（否则同比会缺上年同期、ROE 也无从对比）。
        """
        income_by_period = {row["period_end"]: row for row in income_rows}
        balance_by_period = {row["period_end"]: row for row in balance_rows}
        cash_by_period = {row["period_end"]: row for row in cash_flow_rows}
        periods = sorted(set(income_by_period) | set(balance_by_period))

        # 同比按「相同报告期类型、上一年度」比较，避免 Q1 与 FY 直接相减
        history = {
            (row.get("period_type"), row.get("fiscal_year")): row for row in income_rows
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
                (income.get("period_type"), (income.get("fiscal_year") or 0) - 1), {}
            )
            previous_revenue = previous.get("revenue") or previous.get("total_revenue")
            previous_profit = previous.get("net_income")
            period_type = income.get("period_type") or {
                3: "Q1",
                6: "H1",
                9: "Q3",
                12: "FY",
            }.get(period.month, "UNKNOWN")
            result.append(
                {
                    "ts": period,
                    "period_end": period,
                    "code": code,
                    "report_type": income.get("report_type") or balance.get("report_type"),
                    "fiscal_year": income.get("fiscal_year") or period.year,
                    "period_type": period_type,
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
                    "revenue_yoy": _yoy(revenue, previous_revenue),
                    "net_income_yoy": _yoy(net_income, previous_profit),
                }
            )
        return result
