"""因子命名规范（自研，不与 原始名称一致）。

命名约定
--------
``<category>.<语义>[_<窗口>]``，窗口统一写成 ``_21d`` / ``_21_252``（短期_长期）。

- 语义词使用完整英文，不用 ``gpm/npm/opm/icr`` 这类缩写；
- 基准一律写成 ``_csi300`` / ``_sh``，**不出现指数代码**；
-  原始名只保留在 ``factors/catalog`` 里做对账，不进 factor_id。

示例::

    avg_turnover_5d       -> liquidity.turnover_ma_5d
    return_21d            -> momentum.ret_21d
    alpha_125d_000300     -> momentum.alpha_csi300_125d
    sigma_1320d_000300    -> risk.ivol_csi300_1320d
    gpm_ttm               -> quality.gross_margin_ttm
    earnings_to_price     -> value.ep
"""

from __future__ import annotations

import re

#: 语义词替换（长词优先，避免子串误替换）
_TOKENS: tuple[tuple[str, str], ...] = (
    ("small_cap_reversal", "ret"),
    ("days_beyond_upper_lower", "band_break"),
    ("price_position_ir", "intraday_pos_ir"),
    ("beta_consistency", "beta_ivol"),
    ("adjusted_sharpe", "sharpe_adj"),
    ("bias_std_turn", "turnover_std_bias"),
    ("std_turnover", "turnover_std"),
    ("avg_turnover", "turnover_ma"),
    ("turnover_ma", "turnover_to_mv"),
    ("sum_abs_rtn_amount", "absret_amount"),
    ("volume_alpha", "vol_alpha"),
    ("volume_beta", "vol_beta"),
    ("return_std", "vol"),
    ("days_down_up", "updown_streak"),
    ("high_low", "high_low"),
    ("price_dist", "round_gap"),
    ("amount_ma", "amount_ma"),
    ("bias_turn", "turnover_bias"),
    ("alpha", "alpha"),
    ("beta", "beta"),
    ("sigma", "ivol"),
    ("return", "ret"),
    ("simple", "simple"),
)

_BENCH = {"000300": "csi300", "000001": "sh", "000905": "csi500", "000852": "csi1000"}

#: 财务因子缩写 -> 完整语义
_FIN_TOKENS: tuple[tuple[str, str], ...] = (
    ("earnings_to_price", "ep"),
    ("book_to_market", "bp"),
    ("sales_to_market", "sp"),
    ("ocf_to_market", "ocf_p"),
    ("fcf_to_market", "fcf_p"),
    ("ebitda_to_market", "ebitda_p"),
    ("ncf_to_market", "ncf_p"),
    ("earnings_cut_to_market", "cut_ep"),
    ("dividend_yield_3y_avg", "div_yield_3y"),
    ("quality_composite", "composite"),
    ("cash_profit_ratio", "ocf_to_profit"),
    ("np_to_total_expenses", "profit_to_expense"),
    ("np_to_inventory", "profit_to_inventory"),
    ("np_to_fixed_assets", "profit_to_fixed"),
    ("np_to_deferred_tax", "profit_to_deferred_tax"),
    ("np_to_salary", "profit_to_salary"),
    ("expenses_to_equity", "expense_to_equity"),
    ("income_tax_yoy", "tax_yoy"),
    ("tax_surcharge_yoy", "tax_surcharge_yoy"),
    ("debt_asset_ratio", "debt_to_asset"),
    ("market_value_leverage", "mv_leverage"),
    ("financial_leverage", "fin_leverage"),
    ("gpm", "gross_margin"),
    ("npm_tsh", "parent_roe_tsh"),
    ("npm", "net_margin"),
    ("opm", "operating_margin"),
    ("icr", "interest_cover"),
    ("cfcr", "cash_interest_cover"),
    ("opt_tpro", "op_to_pretax"),
    ("asset_turnover", "asset_turnover"),
    ("fixed_asset_turnover", "fixed_turnover"),
    ("equity_turnover", "equity_turnover"),
    ("inventory_turnover", "inventory_turnover"),
    ("receivable_turnover", "receivable_turnover"),
    ("ar_ap_to_revenue", "ar_ap_to_revenue"),
    ("lra_yoy", "longterm_receivable_yoy"),
    ("eps", "eps"),
    ("roe", "roe"),
    ("roa", "roa"),
    ("de", "de"),
)

_SPECIAL = {
    "dif": "macd_dif",
    "dea": "macd_dea",
    "MACD": "macd",
    "rsrs": "rsrs",
    "rsi": "rsi_14d",
    "log_price": "log_price",
    "size": "mv",
    "float_size": "float_mv",
    "nl_size": "nl_mv",
    "sharpe_60d": "sharpe_60d",
    "sharpe_750d": "sharpe_750d",
}

_CACHE: dict[int, tuple[re.Pattern, dict[str, str]]] = {}

def _replace_tokens(text: str, tokens: tuple[tuple[str, str], ...]) -> str:
    """单次扫描替换：避免 A->B 之后 B 又被 C->D 二次改写（会撞名）。"""
    key = id(tokens)
    cached = _CACHE.get(key)
    if cached is None:
        mapping = dict(tokens)
        pattern = re.compile("|".join(sorted((re.escape(k) for k in mapping), key=len, reverse=True)))
        _CACHE[key] = (pattern, mapping)
        cached = (pattern, mapping)
    pattern, mapping = cached
    return pattern.sub(lambda m: mapping[m.group(0)], text)

def _map_benchmark(text: str) -> str:
    out = text
    for code, alias in _BENCH.items():
        out = out.replace(code, alias)
    return out

def house_short(category: str, source_name: str) -> str:
    """原始因子名 -> 自研短名（不含 category 前缀）。"""
    name = str(source_name)
    if name in _SPECIAL:
        base = _SPECIAL[name]
    else:
        base = _map_benchmark(name)
        tokens = _FIN_TOKENS if category in ("quality", "value", "growth") else _TOKENS
        base = _replace_tokens(base, tokens)
        # 统一窗口写法：_21d_252d -> _21_252；alpha_csi300_125d 保持
        base = re.sub(r"_(\d+)d_(\d+)d$", r"_\1_\2", base)
        base = re.sub(r"_(\d+)d$", r"_\1d", base)
    base = re.sub(r"_+", "_", base).strip("_").lower()
    return base

def house_id(category: str, source_name: str) -> str:
    return f"{category}.{house_short(category, source_name)}"

__all__ = ["house_id", "house_short"]
