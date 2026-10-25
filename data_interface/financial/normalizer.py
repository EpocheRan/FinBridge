"""财务数据标准化。

职责：
1. 证券代码统一（600519 / 600519.SH -> 600519）
2. 数据源字段 -> canonical 字段
3. 日期、单位、正负号统一
4. 派生字段补齐（毛利润、EBITDA、自由现金流、总债务等）
5. 生成确定性 ``report_id``，支持 QuestDB DEDUP 和重述版本
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any

from .fields import (
    REPORT_TYPE_MAP,
    STATEMENT_MAP,
    TUSHARE_FINA_INDICATOR_MAP,
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def normalize_code(code: str) -> str:
    """600519.SH / sh.600519 / 600519 -> 600519。"""
    if code is None:
        return ""
    text = str(code).strip()
    if "." in text:
        parts = text.split(".")
        # sh.600519 / 600519.SH 两种形态都取 6 位部分
        for part in parts:
            if part.isdigit():
                return part
        return parts[0]
    return text


def to_ts_code(code: str) -> str:
    """6 位代码 -> Tushare ts_code。"""
    code = normalize_code(code)
    if not code:
        return ""
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "1", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SH"


def to_baostock_code(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("60", "68", "51", "58", "50")):
        return f"sh.{code}"
    if code.startswith(("4", "8")):
        return f"bj.{code}"
    return f"sz.{code}"


def parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    try:
        import pandas as pd

        parsed = pd.to_datetime(text, errors="coerce")
        return None if pd.isna(parsed) else parsed.to_pydatetime()
    except Exception:
        return None


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def period_type_from_date(period_end: datetime | None) -> str:
    if period_end is None:
        return "UNKNOWN"
    return {3: "Q1", 6: "H1", 9: "Q3", 12: "FY"}.get(period_end.month, "UNKNOWN")


def fiscal_year_from_date(period_end: datetime | None) -> int:
    return period_end.year if period_end else 0


def report_type_from_tushare(value: Any) -> str:
    return REPORT_TYPE_MAP.get(str(value), "consolidated")


def make_report_id(
    code: str,
    statement_type: str,
    period_end: datetime | None,
    report_type: str,
    publish_date: datetime | None,
    source: str,
) -> str:
    raw = "|".join(
        [
            normalize_code(code),
            statement_type,
            period_end.strftime("%Y-%m-%d") if period_end else "",
            report_type,
            publish_date.strftime("%Y-%m-%d") if publish_date else "",
            source,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 报表标准化
# ---------------------------------------------------------------------------
def _base_statement_row(
    statement_type: str,
    raw: dict[str, Any],
    *,
    source: str,
    ingested_at: datetime | None = None,
) -> dict[str, Any] | None:
    code = normalize_code(raw.get("ts_code") or raw.get("code") or "")
    period_end = parse_date(raw.get("end_date") or raw.get("statDate"))
    if not code or period_end is None:
        return None

    publish_date = parse_date(
        raw.get("f_ann_date") or raw.get("ann_date") or raw.get("pubDate")
    )
    report_type = report_type_from_tushare(raw.get("report_type"))
    row: dict[str, Any] = {
        "period_end": period_end,
        "statement_type": statement_type,
        "code": code,
        "publish_date": publish_date,
        "period_type": period_type_from_date(period_end),
        "report_type": report_type,
        "fiscal_year": fiscal_year_from_date(period_end),
        "report_id": make_report_id(
            code, statement_type, period_end, report_type, publish_date, source
        ),
        "source": source,
        "ingested_at": ingested_at or datetime.utcnow(),
        "quality_status": "OK" if publish_date else "PARTIAL",
    }

    field_map = STATEMENT_MAP[statement_type]
    for source_field, canonical_field in field_map.items():
        row[canonical_field] = safe_float(raw.get(source_field))
    return row


def _fill_income_derived(row: dict[str, Any]) -> None:
    revenue = row.get("revenue")
    cost = row.get("cost_of_revenue")
    if row.get("gross_profit") is None and revenue is not None and cost is not None:
        row["gross_profit"] = revenue - cost

    selling = row.get("selling_expense")
    admin = row.get("general_and_admin_expense")
    if (
        row.get("selling_general_admin_expense") is None
        and selling is not None
        and admin is not None
    ):
        row["selling_general_admin_expense"] = selling + admin

    if row.get("ebit") is None and row.get("pretax_profit") is not None:
        row["ebit"] = row["pretax_profit"] + (row.get("interest_expense") or 0.0)

    if row.get("ebitda") is None and row.get("ebit") is not None:
        row["ebitda"] = row["ebit"] + (
            row.get("depreciation_and_amortization") or 0.0
        )


def _fill_balance_derived(row: dict[str, Any]) -> None:
    cash = row.get("cash_and_cash_equivalents")
    short_inv = row.get("short_term_investments")
    if (
        row.get("cash_and_short_term_investments") is None
        and cash is not None
    ):
        row["cash_and_short_term_investments"] = cash + (short_inv or 0.0)

    short_debt = (row.get("short_term_debt") or 0.0) + (
        row.get("current_portion_of_long_term_debt") or 0.0
    )
    long_debt = (row.get("long_term_debt") or 0.0) + (
        row.get("bonds_payable") or 0.0
    )
    if row.get("total_debt") is None and (short_debt or long_debt):
        row["total_debt"] = short_debt + long_debt
    if row.get("net_debt") is None and row.get("total_debt") is not None:
        row["net_debt"] = row["total_debt"] - (cash or 0.0)


def _fill_cash_flow_derived(row: dict[str, Any]) -> None:
    ocf = row.get("operating_cash_flow")
    capex = row.get("capital_expenditure")
    if row.get("free_cash_flow") is None and ocf is not None:
        row["free_cash_flow"] = ocf - (capex or 0.0)


def _validate_statement(statement_type: str, row: dict[str, Any]) -> str:
    status = row.get("quality_status", "OK")
    if statement_type == "BS":
        assets = row.get("total_assets")
        liabilities = row.get("total_liabilities")
        equity = row.get("total_equity")
        if assets and liabilities is not None and equity is not None:
            diff = abs(assets - liabilities - equity)
            if abs(assets) > 0 and diff / abs(assets) > 0.01:
                status = "SUSPECT"
    elif statement_type == "IS":
        revenue = row.get("revenue")
        cost = row.get("cost_of_revenue")
        gross = row.get("gross_profit")
        if revenue and cost is not None and gross is not None:
            if abs(revenue - cost - gross) > max(1.0, abs(revenue) * 0.01):
                status = "SUSPECT"
    return status


def normalize_statement(
    statement_type: str,
    raw: dict[str, Any],
    *,
    source: str = "tushare",
    ingested_at: datetime | None = None,
) -> dict[str, Any] | None:
    row = _base_statement_row(
        statement_type, raw, source=source, ingested_at=ingested_at
    )
    if row is None:
        return None
    if statement_type == "IS":
        _fill_income_derived(row)
    elif statement_type == "BS":
        _fill_balance_derived(row)
    elif statement_type == "CF":
        _fill_cash_flow_derived(row)
    row["quality_status"] = _validate_statement(statement_type, row)
    return row


def merge_fina_indicator(
    statement_rows: list[dict[str, Any]],
    indicator_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 Tushare ``fina_indicator`` 的补充字段合并进报表行。"""
    by_period: dict[str, dict[str, Any]] = {}
    for raw in indicator_rows:
        key = str(raw.get("end_date") or "")
        if key:
            by_period[key] = raw

    for row in statement_rows:
        key = row["period_end"].strftime("%Y%m%d")
        fina = by_period.get(key)
        if not fina:
            continue
        for source_field, canonical_field in TUSHARE_FINA_INDICATOR_MAP.items():
            value = safe_float(fina.get(source_field))
            if value is not None and row.get(canonical_field) is None:
                row[canonical_field] = value

        # fina_indicator 给出的是净债务/带息债务，补齐后再重算派生值
        if row.get("total_debt") is not None and row.get("net_debt") is None:
            row["net_debt"] = row["total_debt"] - (
                row.get("cash_and_cash_equivalents") or 0.0
            )
    return statement_rows


# ---------------------------------------------------------------------------
# 每日估值标准化
# ---------------------------------------------------------------------------
def normalize_daily_basic(
    raw: dict[str, Any],
    *,
    source: str = "tushare_daily_basic",
    ingested_at: datetime | None = None,
) -> dict[str, Any] | None:
    code = normalize_code(raw.get("ts_code") or raw.get("code") or "")
    ts = parse_date(raw.get("trade_date") or raw.get("ts"))
    if not code or ts is None:
        return None

    # Tushare daily_basic: 总股本/总市值单位是“万”，统一成“股/元”
    total_share = safe_float(raw.get("total_share"))
    total_mv = safe_float(raw.get("total_mv"))
    circ_mv = safe_float(raw.get("circ_mv"))
    if total_share is not None:
        total_share *= 10_000
    if total_mv is not None:
        total_mv *= 10_000
    if circ_mv is not None:
        circ_mv *= 10_000

    dividend_yield = safe_float(raw.get("dv_ttm"))
    if dividend_yield is not None:
        # Tushare dv_ttm 单位是百分数（3.59 表示 3.59%），统一转小数
        dividend_yield /= 100.0

    return {
        "ts": ts,
        "code": code,
        "close": safe_float(raw.get("close")),
        "total_share": total_share,
        "market_cap": total_mv,
        "float_market_cap": circ_mv,
        "enterprise_value": None,
        "pe_ttm": safe_float(raw.get("pe_ttm")),
        "pb_mrq": safe_float(raw.get("pb")),
        "ps_ttm": safe_float(raw.get("ps_ttm")),
        "pcf_ttm": None,
        "pfcf": None,
        "ev_ebitda": None,
        "ev_ebit": None,
        "ev_sales": None,
        "ev_gross_profit": None,
        "ev_fcf": None,
        "p_nwc": None,
        "earnings_yield": None,
        "dividend_yield": dividend_yield,
        "market_cap_change_pct": None,
        "source": source,
        "formula_version": "v1",
        "ingested_at": ingested_at or datetime.utcnow(),
    }


# ---------------------------------------------------------------------------
# DB 行转换
# ---------------------------------------------------------------------------
def row_to_values(row: dict[str, Any], columns: list[str]) -> tuple:
    return tuple(row.get(column) for column in columns)
