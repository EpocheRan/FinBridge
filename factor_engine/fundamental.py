"""基本面成长/质量因子（15 个）。

数据链路：
- 报表：``fin_income_statement`` / ``fin_balance_sheet`` / ``fin_cash_flow``
  （全历史，按 ``publish_date`` 做 Point-in-Time）。
- TTM/单季：复用 ``data_interface.financial.metrics``。
- 每日估值：``fin_valuation_daily`` 提供 ``close`` / ``total_share``。

输出：``ts, code, factor_id, value`` 长表，交 ``factor_engine`` 落库。
所有因子标记 ``materialization=STORED``（依赖基本面 + 全市场截面排名，回测只读）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.financial.metrics import (
    CASH_FLOW_TTM_FIELDS,
    INCOME_TTM_FIELDS,
    latest_row,
    quarterize,
    ttm,
)
from data_interface.financial.repository import FinancialRepository
from factors.spec import Dependency, FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category

from .fin_ext import FIN_EXT_SPECS, RANK_EXT, compute_ext

logger = logging.getLogger("FactorEngine.Fundamental")

_SOURCE = SourceSpec("fin_income_statement", "questdb", ("net_income",), "available_at")

#: factor_id -> 是否做截面排名
RANK_FACTORS = {
    "growth.np_ttm_qoq": True,
    "growth.yoy_ocf": True,
    "growth.sa": True,
    "quality.gross_margin_qoq": True,
    "quality.pa": True,
    "quality.yoy_roa": True,
    "growth.yoy_net_asset": True,
    "quality.yoy_roe": True,
    "growth.yoy_total_asset": True,
    "growth.eaa": True,
    "growth.eap": True,
    "growth.asset_growth_qoq": True,
}

FUNDAMENTAL_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        factor_id="valuation.peg_252d", name="PEG", category=Category.VALUATION,
        subcategory="earnings", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="negative", price_basis="raw", version="v1",
        source_version_policy="fin", warmup=252,
        formula="PE/(EPS_growth*100)", aliases=("value.peg_252d", "peg_252d"),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.np_ttm_qoq", name="净利润TTM环比", category=Category.GROWTH,
        subcategory="profit", source=_SOURCE, window=1, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(NetProfit_TTM_t/NetProfit_TTM_{t-1}-1)", aliases=("np_ttm_qoq",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.yoy_net_profit", name="净利润同比", category=Category.GROWTH,
        subcategory="profit", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="NetProfit_t/NetProfit_{t-252}-1", aliases=("yoy_net_profit",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.yoy_ocf", name="经营现金流同比", category=Category.GROWTH,
        subcategory="asset_cashflow", source=SourceSpec("fin_cash_flow", "questdb", ("operating_cash_flow",), "available_at"),
        window=252, freq="1d", value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(OCF_TTM_t/OCF_TTM_{t-252}-1)", aliases=("yoy_ocf",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.sa", name="每股营收增速加速度", category=Category.GROWTH,
        subcategory="revenue", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=315,
        formula="rank(SG - SG_{t-63})", aliases=("sa",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="quality.gross_margin_qoq", name="毛利率TTM环比", category=Category.QUALITY,
        subcategory="profitability", source=_SOURCE, window=63, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(GrossMargin/GrossMargin_{t-63}-1)", aliases=("gross_margin_qoq",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="quality.pa", name="ROA增长加速度", category=Category.QUALITY,
        subcategory="profitability", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=315,
        formula="rank(PG - PG_{t-63})", aliases=("pa",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="quality.yoy_roa", name="ROA同比", category=Category.QUALITY,
        subcategory="profitability", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(ROA_t/ROA_{t-252}-1)", aliases=("yoy_roa",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.yoy_net_asset", name="净资产同比", category=Category.GROWTH,
        subcategory="asset_cashflow", source=SourceSpec("fin_balance_sheet", "questdb", ("total_equity",), "available_at"),
        window=252, freq="1d", value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(NetAsset_t/NetAsset_{t-252}-1)", aliases=("yoy_net_asset",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.yoy_revenue", name="营业收入同比", category=Category.GROWTH,
        subcategory="revenue", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="Revenue_t/Revenue_{t-252}-1", aliases=("yoy_revenue",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="quality.yoy_roe", name="ROE同比", category=Category.QUALITY,
        subcategory="profitability", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(ROE_t/ROE_{t-252}-1)", aliases=("yoy_roe",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.yoy_total_asset", name="总资产同比", category=Category.GROWTH,
        subcategory="asset_cashflow", source=SourceSpec("fin_balance_sheet", "questdb", ("total_assets",), "available_at"),
        window=252, freq="1d", value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=252,
        formula="rank(TotalAsset_t/TotalAsset_{t-252}-1)", aliases=("yoy_total_asset",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.eaa", name="EPS增长加速度", category=Category.GROWTH,
        subcategory="profit", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=315,
        formula="rank(EGA - EGA_{t-63})", aliases=("eaa",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.eap", name="EPS价格增长加速度", category=Category.GROWTH,
        subcategory="profit", source=_SOURCE, window=252, freq="1d",
        value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="raw", version="v1",
        source_version_policy="fin", warmup=315,
        formula="rank(EGP - EGP_{t-63})", aliases=("eap",),
        materialization=Materialization.STORED,
    ),
    FactorSpec(
        factor_id="growth.asset_growth_qoq", name="总资产环比", category=Category.GROWTH,
        subcategory="asset_cashflow", source=SourceSpec("fin_balance_sheet", "questdb", ("total_assets",), "available_at"),
        window=1, freq="1d", value_kind="daily", update_mode="event", unit="ratio",
        direction="positive", price_basis="none", version="v1",
        source_version_policy="fin", warmup=1,
        formula="rank(TotalAssets_t/TotalAssets_{t-1}-1)", aliases=("asset_growth_qoq",),
        materialization=Materialization.STORED,
    ),
)


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    try:
        return a / b
    except ZeroDivisionError:
        return None


def _single_quarter(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
    quarters = [row for row in quarterize(rows, fields) if row.get("_single")]
    if not quarters:
        return {}
    return quarters[-1]


class FundamentalComputer:
    """加载并计算基本面因子。"""

    def __init__(self, config: dict[str, Any] | None = None, repository: Any | None = None):
        self.config = config or get_config()
        self.repository = repository or FinancialRepository(
            QuestDBClient(self.config, connect=False)
        )

    # ------------------------------------------------------------------ 数据
    def _report_snapshots(
        self,
        income: list[dict[str, Any]],
        balance: list[dict[str, Any]],
        cash: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        periods = sorted(
            {row["period_end"] for row in income + balance + cash if row.get("period_end")}
        )
        snaps: list[dict[str, Any]] = []
        for period in periods:
            ih = [row for row in income if row["period_end"] <= period]
            bh = [row for row in balance if row["period_end"] <= period]
            ch = [row for row in cash if row["period_end"] <= period]
            if not ih:
                continue
            itm = ttm(ih, INCOME_TTM_FIELDS)
            ctm = ttm(ch, CASH_FLOW_TTM_FIELDS)
            bal = latest_row(bh)
            sq = _single_quarter(ih, ["revenue", "net_income", "cost_of_revenue", "basic_eps"])

            recent = sorted(ih, key=lambda item: item["period_end"])[-4:]
            publish_candidates = [
                row.get("publish_date") or row["period_end"] for row in recent
            ]
            publish_candidates.append(bal.get("publish_date") or bal.get("period_end") or period)
            publish = max(publish_candidates)

            snaps.append(
                {
                    "period_end": period,
                    "publish_date": publish,
                    "net_income_ttm": itm.get("net_income"),
                    "net_income_parent_ttm": itm.get("net_income_attributable_to_parent"),
                    "revenue_ttm": itm.get("revenue") or itm.get("total_revenue"),
                    "cost_ttm": itm.get("cost_of_revenue"),
                    "ocf_ttm": ctm.get("operating_cash_flow"),
                    "total_assets": bal.get("total_assets"),
                    "total_equity": bal.get("total_equity"),
                    "revenue_q": sq.get("revenue"),
                    "net_income_q": sq.get("net_income"),
                    # ---- 扩展字段（供 Quality 55 / Value 8 使用）
                    "gross_profit_ttm": itm.get("gross_profit"),
                    "operating_profit_ttm": itm.get("operating_profit"),
                    "ebit_ttm": itm.get("ebit"),
                    "ebitda_ttm": itm.get("ebitda"),
                    "pretax_profit_ttm": itm.get("pretax_profit"),
                    "income_tax_ttm": itm.get("income_tax"),
                    "interest_expense_ttm": itm.get("interest_expense"),
                    "selling_ttm": itm.get("selling_expense"),
                    "admin_ttm": itm.get("general_and_admin_expense"),
                    "finance_expense_ttm": itm.get("finance_expense"),
                    "operating_profit_q": sq.get("operating_profit"),
                    "pretax_profit_q": sq.get("pretax_profit"),
                    "total_current_assets": bal.get("total_current_assets"),
                    "total_current_liabilities": bal.get("total_current_liabilities"),
                    "inventory": bal.get("inventory"),
                    "accounts_receivable": bal.get("accounts_receivable"),
                    "fixed_assets": bal.get("fixed_assets"),
                    "cash": bal.get("cash_and_cash_equivalents"),
                    "short_term_investments": bal.get("short_term_investments"),
                    "total_liabilities": bal.get("total_liabilities"),
                    "total_debt": bal.get("total_debt"),
                    "equity_parent": bal.get("equity_attributable_to_parent"),
                    "non_current_liabilities": bal.get("total_non_current_liabilities"),
                    "investing_cf_ttm": ctm.get("investing_cash_flow"),
                    "financing_cf_ttm": ctm.get("financing_cash_flow"),
                    "capex_ttm": ctm.get("capital_expenditure"),
                    "dividends_paid_ttm": ctm.get("dividends_paid"),
                }
            )
        return snaps

    def load_metrics(
        self,
        codes: Sequence[str],
        start,
        end,
        trade_dates: Sequence | pd.DatetimeIndex,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for code in codes:
            code = str(code)
            valuation = self.repository.get_valuation(
                code, start_date=str(pd.Timestamp(start).date()), end_date=str(pd.Timestamp(end).date())
            )
            val = pd.DataFrame(valuation) if valuation else pd.DataFrame()
            snaps = self._report_snapshots(
                self.repository.get_statements(code, "IS"),
                self.repository.get_statements(code, "BS"),
                self.repository.get_statements(code, "CF"),
            )
            frame = self._align_code(code, val, snaps, trade_dates)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame(
                columns=["ts", "code", "close", "total_share", "eps_ttm", "net_profit_ttm",
                         "ocf_ttm", "revenue_ttm", "cost_ttm", "roa", "roe", "net_asset",
                         "total_asset", "sps", "eps_q", "revenue_q"]
            )
        return pd.concat(frames, ignore_index=True)

    def _align_code(self, code, valuation, snaps, trade_dates):
        dates = pd.DatetimeIndex(pd.to_datetime(trade_dates)).normalize()
        metric_cols = [
            "net_income_ttm", "net_income_parent_ttm", "revenue_ttm", "cost_ttm", "ocf_ttm",
            "total_assets", "total_equity", "revenue_q", "net_income_q",
            "gross_profit_ttm", "operating_profit_ttm", "ebit_ttm", "ebitda_ttm",
            "pretax_profit_ttm", "income_tax_ttm", "interest_expense_ttm",
            "selling_ttm", "admin_ttm", "finance_expense_ttm",
            "operating_profit_q", "pretax_profit_q",
            "total_current_assets", "total_current_liabilities", "inventory",
            "accounts_receivable", "fixed_assets", "cash", "short_term_investments",
            "total_liabilities", "total_debt", "equity_parent", "non_current_liabilities",
            "investing_cf_ttm", "financing_cf_ttm", "capex_ttm", "dividends_paid_ttm",
        ]
        base = pd.DataFrame({"ts": dates, "code": code})
        if valuation is not None and not valuation.empty:
            v = valuation.copy()
            v["ts"] = pd.to_datetime(v["ts"], errors="coerce").dt.normalize()
            for col in ("close", "total_share"):
                if col in v.columns:
                    v[col] = pd.to_numeric(v[col], errors="coerce")
            v = v[["ts", "close", "total_share"]].drop_duplicates("ts", keep="last")
            base = base.merge(v, on="ts", how="left")

        if snaps:
            s = pd.DataFrame(snaps)
            s["publish_date"] = pd.to_datetime(s["publish_date"], errors="coerce")
            s = s.dropna(subset=["publish_date"]).sort_values("publish_date")
            aligned = pd.merge_asof(
                pd.DataFrame({"ts": dates}).sort_values("ts"),
                s[["publish_date", *metric_cols]].rename(columns={"publish_date": "ts"}),
                on="ts",
                direction="backward",
            )
            base = base.merge(aligned, on="ts", how="left")

        if "close" not in base.columns:
            base["close"] = None
        if "total_share" not in base.columns:
            base["total_share"] = None
        # 无报表快照（新上市/未同步财务）时 snaps 为空，上面不会 merge 出这些列；
        # 补成 NaN 列，避免 pd.to_numeric(None) 退化成标量后 .div 报 AttributeError。
        for col in metric_cols:
            if col not in base.columns:
                base[col] = float("nan")
        ts = pd.to_numeric(base["total_share"], errors="coerce")
        base["eps_ttm"] = pd.to_numeric(base.get("net_income_ttm"), errors="coerce").div(ts)
        base["net_profit_ttm"] = pd.to_numeric(base.get("net_income_ttm"), errors="coerce")
        base["ocf_ttm"] = pd.to_numeric(base.get("ocf_ttm"), errors="coerce")
        base["revenue_ttm"] = pd.to_numeric(base.get("revenue_ttm"), errors="coerce")
        base["cost_ttm"] = pd.to_numeric(base.get("cost_ttm"), errors="coerce")
        base["roa"] = base["net_profit_ttm"].div(pd.to_numeric(base.get("total_assets"), errors="coerce"))
        base["roe"] = base["net_profit_ttm"].div(pd.to_numeric(base.get("total_equity"), errors="coerce"))
        base["net_asset"] = pd.to_numeric(base.get("total_equity"), errors="coerce")
        base["total_asset"] = pd.to_numeric(base.get("total_assets"), errors="coerce")
        base["sps"] = pd.to_numeric(base.get("revenue_q"), errors="coerce").div(ts)
        base["eps_q"] = pd.to_numeric(base.get("net_income_q"), errors="coerce").div(ts)
        base["revenue_q"] = pd.to_numeric(base.get("revenue_q"), errors="coerce")
        ordered: list[str] = [
            "ts", "code", "close", "total_share", "eps_ttm", "net_profit_ttm",
            "ocf_ttm", "revenue_ttm", "cost_ttm", "roa", "roe", "net_asset",
            "total_asset", "sps", "eps_q", "revenue_q",
        ]
        for column in metric_cols:      # 去重并保序：metric_cols 与显式列表有交集
            if column not in ordered:
                ordered.append(column)
        return base[ordered]

    # ------------------------------------------------------------------ 计算
    def compute(self, metrics: pd.DataFrame) -> pd.DataFrame:
        """metrics(ts, code, ...) -> 长表(ts, code, factor_id, value)。"""
        if metrics is None or metrics.empty:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])

        frames: list[pd.DataFrame] = []
        factor_ids = [spec.factor_id for spec in FUNDAMENTAL_SPECS] + [
            spec.factor_id for spec in FIN_EXT_SPECS
        ]
        for code, group in metrics.sort_values("ts").groupby("code"):
            g = group.set_index("ts").sort_index()
            eps_growth = g["eps_ttm"] / g["eps_ttm"].shift(252) - 1.0
            values = {
                "valuation.peg_252d": (g["close"] / g["eps_ttm"]) / (eps_growth * 100.0),
                "growth.np_ttm_qoq": g["net_profit_ttm"] / g["net_profit_ttm"].shift(1) - 1.0,
                "growth.yoy_net_profit": g["net_profit_ttm"] / g["net_profit_ttm"].shift(252) - 1.0,
                "growth.yoy_ocf": g["ocf_ttm"] / g["ocf_ttm"].shift(252) - 1.0,
                "growth.sa": (lambda s: s - s.shift(63))(
                    g["sps"] / g["sps"].shift(252) - 1.0
                ),
                "quality.gross_margin_qoq": (lambda m: m / m.shift(63) - 1.0)(
                    (g["revenue_ttm"] - g["cost_ttm"]) / g["revenue_ttm"]
                ),
                "quality.pa": (lambda p: p - p.shift(63))(
                    g["roa"] - g["roa"].shift(252)
                ),
                "quality.yoy_roa": g["roa"] / g["roa"].shift(252) - 1.0,
                "growth.yoy_net_asset": g["net_asset"] / g["net_asset"].shift(252) - 1.0,
                "growth.yoy_revenue": g["revenue_ttm"] / g["revenue_ttm"].shift(252) - 1.0,
                "quality.yoy_roe": g["roe"] / g["roe"].shift(252) - 1.0,
                "growth.yoy_total_asset": g["total_asset"] / g["total_asset"].shift(252) - 1.0,
                "growth.eaa": (lambda e: e - e.shift(63))(
                    g["eps_q"] / g["eps_q"].shift(252) - 1.0
                ),
                "growth.eap": (lambda e: e - e.shift(63))(
                    (g["eps_q"] - g["eps_q"].shift(252)) / g["close"]
                ),
                "growth.asset_growth_qoq": g["total_asset"] / g["total_asset"].shift(1) - 1.0,
            }
            values.update(compute_ext(g))
            for factor_id in factor_ids:
                frames.append(
                    pd.DataFrame(
                        {"ts": g.index, "code": code, "factor_id": factor_id,
                         "value": values[factor_id].to_numpy()}
                    )
                )
        long = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["ts", "code", "factor_id", "value"]
        )

        # 截面排名（每交易日、每因子，在全体 code 间）
        rank_ids = set(RANK_FACTORS) | set(RANK_EXT)
        rank_mask = long["factor_id"].isin(rank_ids)
        ranked = long[rank_mask].copy()
        if not ranked.empty:
            ranked["value"] = ranked.groupby(["ts", "factor_id"])["value"].rank(
                pct=True, na_option="keep"
            )
        return pd.concat([long[~rank_mask], ranked], ignore_index=True)
