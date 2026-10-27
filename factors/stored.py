"""screener_daily 因子元数据（stored 路径的占位）。

MVP 只实现 compute；stored 在接入 screener_daily 覆盖/版本校验后启用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .library import Unit


@dataclass(frozen=True)
class StoredColumn:
    column: str
    unit: Unit
    version: str = "daily-v1"
    price_basis: str = "none"


#: 仅列元数据；price/price_delta 单位不在此表，因为 qfq 与 hfq 相差 per-symbol 常数。
SCREENER_DAILY_COLUMNS: dict[str, StoredColumn] = {
    "pct_chg": StoredColumn("pct_chg", "pct"),
    "gap_pct": StoredColumn("gap_pct", "pct"),
    "intraday_pct": StoredColumn("intraday_pct", "pct"),
    "range_pct": StoredColumn("range_pct", "pct"),
    "atr_pct": StoredColumn("atr_pct", "pct"),
    "volatility_20d": StoredColumn("volatility_20d", "pct"),
    "volatility_60d": StoredColumn("volatility_60d", "pct"),
    "close_vs_ma20_pct": StoredColumn("close_vs_ma20_pct", "pct"),
    "close_vs_ma60_pct": StoredColumn("close_vs_ma60_pct", "pct"),
    "close_vs_ma250_pct": StoredColumn("close_vs_ma250_pct", "pct"),
    "dist_high_52w_pct": StoredColumn("dist_high_52w_pct", "pct"),
    "dist_low_52w_pct": StoredColumn("dist_low_52w_pct", "pct"),
    "beta_252": StoredColumn("beta_252", "ratio"),
    "corr_252": StoredColumn("corr_252", "ratio"),
    "volume_ratio_20d": StoredColumn("volume_ratio_20d", "ratio"),
    "turnover": StoredColumn("turnover", "pct"),
    "total_mv": StoredColumn("total_mv", "currency", price_basis="raw"),
    "float_mv": StoredColumn("float_mv", "currency", price_basis="raw"),
    "total_share": StoredColumn("total_share", "shares"),
    "float_share": StoredColumn("float_share", "shares"),
    "is_new_high_52w": StoredColumn("is_new_high_52w", "bool"),
    "is_new_low_52w": StoredColumn("is_new_low_52w", "bool"),
    "lookback_days": StoredColumn("lookback_days", "days"),
}
