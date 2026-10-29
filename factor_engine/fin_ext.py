"""财务类因子：Quality 55 + Value 8（共 63）。

数据链路
--------
全部基于 ``FundamentalComputer.load_metrics`` 产出的**逐日对齐**面板
（``ts, code, close, total_share, 及各报表字段``），字段已按 ``publish_date`` 做 PIT。

口径
----
- 需要 TTM 的用 ``*_ttm``；需要单季的用 ``*_q``；
- 同比统一用 ``shift(252)``（交易日年），环比用 ``shift(63)``（季度交易日）；
- 除零统一走 :func:`_safe_div`，返回 NaN 而不是 inf；
- 与 与公开因子口径的差异：缺 ``递延所得税资产 / 预收 / 预付 / 长期应收 / 应付职工薪酬 /
  营业税金及附加 / 扣非净利润`` 七类字段，对应 7 个因子未实现（见 ``BLOCKED``）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factors.naming import house_id, house_short
from factors.spec import FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category

_SOURCE = SourceSpec("fin_income_statement", "questdb",
                     ("net_income", "revenue", "total_assets"), "available_at")

#: 明确未实现的因子（缺字段）
BLOCKED: dict[str, str] = {
    "ar_ap_to_revenue": "缺 advance_receipts / advance_payment",
    "np_to_deferred_tax_yoy": "缺 deferred_tax_assets",
    "lra_yoy": "缺 longterm_receivable",
    "np_to_salary_yoy": "缺 staff_behalf_paid",
    "tax_surcharge_yoy": "缺 operating_tax_surcharges",
    "pegh5": "缺 5 年 EPS 历史",
    "etp5": "缺 5 年市值历史",
    "earnings_cut_to_market": "缺扣非净利润",
}

def _safe_div(a, b) -> pd.Series:
    if not isinstance(b, pd.Series):
        b = pd.Series(float(b), index=a.index if isinstance(a, pd.Series) else None)
    return (a / b.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

def _yoy(series: pd.Series, periods: int = 252) -> pd.Series:
    return series / series.shift(periods).replace(0, np.nan) - 1.0

def _qoq(series: pd.Series, periods: int = 1) -> pd.Series:
    return series / series.shift(periods).replace(0, np.nan) - 1.0

def _col(g: pd.DataFrame, name: str) -> pd.Series:
    if name in g.columns:
        return pd.to_numeric(g[name], errors="coerce")
    return pd.Series(np.nan, index=g.index, dtype="float64")

def compute_ext(g: pd.DataFrame) -> dict[str, pd.Series]:
    """给定**单只股票**的逐日面板，返回 factor_id -> Series。"""
    revenue_ttm = _col(g, "revenue_ttm")
    cost_ttm = _col(g, "cost_ttm")
    revenue_q = _col(g, "revenue_q")
    net_ttm = _col(g, "net_income_ttm")
    net_parent_ttm = _col(g, "net_income_parent_ttm")
    net_q = _col(g, "net_income_q")
    ocf = _col(g, "ocf_ttm")
    assets = _col(g, "total_asset")
    equity = _col(g, "equity_parent").fillna(_col(g, "total_equity"))
    equity_parent = _col(g, "equity_parent")
    liabilities = _col(g, "total_liabilities")
    current_assets = _col(g, "total_current_assets")
    current_liab = _col(g, "total_current_liabilities")
    inventory = _col(g, "inventory")
    receivable = _col(g, "accounts_receivable")
    fixed_assets = _col(g, "fixed_assets")
    cash = _col(g, "cash")
    sti = _col(g, "short_term_investments")
    op_ttm = _col(g, "operating_profit_ttm")
    op_q = _col(g, "operating_profit_q")
    pretax_q = _col(g, "pretax_profit_q")
    ebit = _col(g, "ebit_ttm")
    ebitda = _col(g, "ebitda_ttm")
    interest = _col(g, "interest_expense_ttm")
    income_tax = _col(g, "income_tax_ttm")
    selling = _col(g, "selling_ttm")
    admin = _col(g, "admin_ttm")
    finance_exp = _col(g, "finance_expense_ttm")
    capex = _col(g, "capex_ttm")
    icf = _col(g, "investing_cf_ttm")
    fcf_cf = _col(g, "financing_cf_ttm")
    dividends = _col(g, "dividends_paid_ttm")
    close = _col(g, "close")
    shares = _col(g, "total_share")
    market_cap = close * shares

    gpm = _safe_div(revenue_ttm - cost_ttm, revenue_ttm)
    npm_ttm = _safe_div(net_ttm, revenue_ttm)
    opm_ttm = _safe_div(op_ttm, revenue_ttm)
    roa_ttm = _safe_div(net_ttm, assets)
    roe_ttm = _safe_div(net_parent_ttm.fillna(net_ttm), equity)
    current_ratio = _safe_div(current_assets, current_liab)
    quick_ratio = _safe_div(current_assets - inventory, current_liab)
    cash_ratio = _safe_div(cash.fillna(0) + sti.fillna(0), current_liab)
    de = _safe_div(liabilities, equity)
    asset_turnover = _safe_div(revenue_ttm, assets)
    inv_turnover = _safe_div(cost_ttm, inventory)
    rec_turnover = _safe_div(revenue_ttm, receivable)
    fa_turnover = _safe_div(revenue_ttm, fixed_assets)
    equity_turnover = _safe_div(revenue_ttm, equity)

    total_expenses = selling + admin + finance_exp

    values: dict[str, pd.Series] = {
        # ---------------- Quality：盈利能力
        "quality.roe_ttm": roe_ttm,
        "quality.roe_ttm_lag63d": roe_ttm.shift(63),
        "quality.roe_y": roe_ttm,
        "quality.eps_ttm": _col(g, "eps_ttm"),
        "quality.eps_q": _col(g, "eps_q"),
        "quality.eps_y": _col(g, "eps_ttm"),
        "quality.gpm_ttm": gpm,
        "quality.gpm_q": gpm,
        "quality.gpm_y": gpm,
        "quality.npm_ttm": npm_ttm,
        "quality.npm_q": _safe_div(net_q, revenue_q),
        "quality.npm_y": npm_ttm,
        "quality.npm_ttm_qoq": _qoq(npm_ttm, 63),
        "quality.npm_q_qoq": _qoq(_safe_div(net_q, revenue_q), 1),
        "quality.opm_ttm": opm_ttm,
        "quality.opm_y": opm_ttm,
        "quality.roa_ttm": roa_ttm,
        "quality.roa_q": _safe_div(net_q, assets),
        "quality.roa_y": roa_ttm,
        "quality.opt_tpro": _safe_div(op_q, pretax_q),
        "quality.quality_composite": (
            _safe_div(op_ttm, assets) + _safe_div(net_ttm, equity)
            + _safe_div(net_ttm, assets) + _safe_div(ocf + icf, assets)
            + _safe_div(op_ttm, revenue_ttm) + _safe_div(net_ttm, ocf + icf)
        ),
        "quality.asset_turnover": asset_turnover,
        "quality.fixed_asset_turnover": fa_turnover,
        "quality.equity_turnover": equity_turnover,
        "quality.inventory_turnover": inv_turnover,
        "quality.receivable_turnover": rec_turnover,
        # ---------------- Quality：现金流与偿债
        "quality.cash_profit_ratio": _safe_div(ocf - net_ttm, net_ttm),
        "quality.icr": _safe_div(ebit, interest),
        "quality.cfcr": _safe_div(ocf, interest),
        "quality.npm_tsh": _safe_div(net_parent_ttm, equity.shift(63) + equity) * 2,
        "quality.np_to_inventory_yoy": _yoy(_safe_div(net_q, inventory)),
        "quality.np_to_fixed_assets_yoy": _yoy(_safe_div(net_q, fixed_assets)),
        "quality.np_to_total_expenses_yoy": _yoy(_safe_div(net_q, total_expenses)),
        "quality.expenses_to_equity_yoy": _yoy(_safe_div(total_expenses, equity_parent)),
        "quality.income_tax_yoy": _yoy(income_tax),
        "quality.debt_asset_ratio": _safe_div(liabilities, assets),
        "quality.financial_leverage": _safe_div(assets, equity),
        "quality.current_ratio": current_ratio,
        "quality.quick_ratio": quick_ratio,
        "quality.cash_ratio": cash_ratio,
        "quality.de": de,
        "quality.market_value_leverage": _safe_div(
            market_cap - _col(g, "non_current_liabilities"), market_cap),
        # ---------------- Quality：变化量（delta）
        "quality.delta_current_ratio": current_ratio - current_ratio.shift(252),
        "quality.delta_quick_ratio": quick_ratio - quick_ratio.shift(252),
        "quality.delta_cash_ratio": cash_ratio - cash_ratio.shift(252),
        "quality.delta_de": de - de.shift(252),
        "quality.delta_gpm": gpm - gpm.shift(252),
        "quality.delta_npm": npm_ttm - npm_ttm.shift(252),
        "quality.delta_opm": opm_ttm - opm_ttm.shift(252),
        "quality.delta_roa": roa_ttm - roa_ttm.shift(252),
        "quality.delta_roe": roe_ttm - roe_ttm.shift(252),
        "quality.delta_asset_turnover": asset_turnover - asset_turnover.shift(252),
        "quality.delta_inventory_turnover": inv_turnover - inv_turnover.shift(252),
        # ---------------- Value
        "value.earnings_to_price": _safe_div(net_parent_ttm.fillna(net_ttm), market_cap),
        "value.book_to_market": _safe_div(equity, market_cap),
        "value.sales_to_market": _safe_div(revenue_ttm, market_cap),
        "value.ocf_to_market": _safe_div(ocf, market_cap),
        "value.fcf_to_market": _safe_div(ocf - capex, market_cap),
        "value.ebitda_to_market": _safe_div(ebitda, market_cap),
        "value.ncf_to_market": _safe_div(ocf + icf + fcf_cf, market_cap),
        "value.dividend_yield_3y_avg": _safe_div(
            dividends.rolling(735, min_periods=250).sum() / 3.0,
            close * shares),
    }
    # 统一改成自研命名（category.semantic），原始名只留在目录里对账
    return {house_id(key.split(".", 1)[0], key.split(".", 1)[1]): value
            for key, value in values.items()}

def _spec(factor_id: str, name: str, category: Category, subcategory: str,
          direction: str, formula: str) -> FactorSpec:
    return FactorSpec(
        factor_id=f"{category.value}.{house_short(category.value, factor_id.split('.', 1)[1])}",
        name=house_short(category.value, factor_id.split(".", 1)[1]),
        category=category,
        subcategory=subcategory,
        source=_SOURCE,
        window=252,
        freq="1d",
        value_kind="daily",
        update_mode="event",
        unit="ratio",
        direction=direction,  # type: ignore[arg-type]
        price_basis="none",
        version="v1",
        source_version_policy="fin",
        warmup=315,
        formula=formula,
        aliases=(factor_id.split(".", 1)[1],),
        materialization=Materialization.STORED,
    )

_QUALITY_META: tuple[tuple[str, str, str], ...] = (
    # (factor 后缀, subcategory, direction)
    ("roe_ttm", "profitability", "positive"),
    ("roe_ttm_lag63d", "profitability", "positive"),
    ("roe_y", "profitability", "positive"),
    ("eps_ttm", "profitability", "positive"),
    ("eps_q", "profitability", "positive"),
    ("eps_y", "profitability", "positive"),
    ("gpm_ttm", "profitability", "positive"),
    ("gpm_q", "profitability", "positive"),
    ("gpm_y", "profitability", "positive"),
    ("npm_ttm", "profitability", "positive"),
    ("npm_q", "profitability", "positive"),
    ("npm_y", "profitability", "positive"),
    ("npm_ttm_qoq", "profitability", "positive"),
    ("npm_q_qoq", "profitability", "positive"),
    ("opm_ttm", "profitability", "positive"),
    ("opm_y", "profitability", "positive"),
    ("roa_ttm", "profitability", "positive"),
    ("roa_q", "profitability", "positive"),
    ("roa_y", "profitability", "positive"),
    ("opt_tpro", "profitability", "positive"),
    ("quality_composite", "profitability", "positive"),
    ("asset_turnover", "profitability", "positive"),
    ("fixed_asset_turnover", "profitability", "positive"),
    ("equity_turnover", "profitability", "positive"),
    ("inventory_turnover", "profitability", "positive"),
    ("receivable_turnover", "profitability", "positive"),
    ("cash_profit_ratio", "earnings_quality", "positive"),
    ("icr", "earnings_quality", "positive"),
    ("cfcr", "earnings_quality", "positive"),
    ("npm_tsh", "earnings_quality", "positive"),
    ("np_to_inventory_yoy", "earnings_quality", "positive"),
    ("np_to_fixed_assets_yoy", "earnings_quality", "positive"),
    ("np_to_total_expenses_yoy", "earnings_quality", "positive"),
    ("expenses_to_equity_yoy", "earnings_quality", "negative"),
    ("income_tax_yoy", "earnings_quality", "negative"),
    ("debt_asset_ratio", "financial_safety", "negative"),
    ("financial_leverage", "financial_safety", "negative"),
    ("current_ratio", "financial_safety", "positive"),
    ("quick_ratio", "financial_safety", "positive"),
    ("cash_ratio", "financial_safety", "positive"),
    ("de", "financial_safety", "negative"),
    ("market_value_leverage", "financial_safety", "positive"),
    ("delta_current_ratio", "stability", "positive"),
    ("delta_quick_ratio", "stability", "positive"),
    ("delta_cash_ratio", "stability", "positive"),
    ("delta_de", "stability", "negative"),
    ("delta_gpm", "stability", "positive"),
    ("delta_npm", "stability", "positive"),
    ("delta_opm", "stability", "positive"),
    ("delta_roa", "stability", "positive"),
    ("delta_roe", "stability", "positive"),
    ("delta_asset_turnover", "stability", "positive"),
    ("delta_inventory_turnover", "stability", "positive"),
)

_VALUE_META: tuple[tuple[str, str, str], ...] = (
    ("earnings_to_price", "earnings", "positive"),
    ("book_to_market", "asset", "positive"),
    ("sales_to_market", "income", "positive"),
    ("ocf_to_market", "cash_dividend", "positive"),
    ("fcf_to_market", "cash_dividend", "positive"),
    ("ebitda_to_market", "income", "positive"),
    ("ncf_to_market", "cash_dividend", "positive"),
    ("dividend_yield_3y_avg", "cash_dividend", "positive"),
)

QUALITY_EXT_SPECS: tuple[FactorSpec, ...] = tuple(
    _spec(f"quality.{name}", name, Category.QUALITY, sub, direction, f"src:{name}")
    for name, sub, direction in _QUALITY_META
)
VALUE_EXT_SPECS: tuple[FactorSpec, ...] = tuple(
    _spec(f"value.{name}", name, Category.VALUE, sub, direction, f"src:{name}")
    for name, sub, direction in _VALUE_META
)

FIN_EXT_SPECS: tuple[FactorSpec, ...] = QUALITY_EXT_SPECS + VALUE_EXT_SPECS

#: 需要做截面排名的因子（与  的 CrossSectionalRank 对齐）
RANK_EXT: frozenset[str] = frozenset(
    spec.factor_id for spec in FIN_EXT_SPECS
    if spec.factor_id.split(".", 1)[1] not in {
        "roe_ttm", "roe_ttm_lag63d", "roe_y", "debt_asset_ratio", "quick_ratio",
        "current_ratio", "de", "dividend_yield_3y_avg", "earnings_to_price",
        "book_to_market", "ocf_to_market", "fcf_to_market", "ebitda_to_market",
        "ncf_to_market", "log_price",
    }
)

__all__ = [
    "BLOCKED",
    "FIN_EXT_SPECS",
    "QUALITY_EXT_SPECS",
    "RANK_EXT",
    "VALUE_EXT_SPECS",
    "compute_ext",
]
