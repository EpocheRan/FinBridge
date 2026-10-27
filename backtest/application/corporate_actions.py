"""公司行为近似：从 adj_factor 变动推导。

只有 ``adj_factor`` 时无法区分现金分红与送转，统一按“股数比例”处理：
- 除权日 shares *= f_new / f_prev；
- 复权收益连续，但现金分红被当作再投资，需在 meta 标记 approximation。
"""

from __future__ import annotations

import pandas as pd


def derive_approximate_actions(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "symbol", "cash_div", "share_ratio"]
    if frame is None or frame.empty or "adj_factor" not in frame.columns:
        return pd.DataFrame(columns=columns)
    data = frame[["date", "symbol", "adj_factor"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["adj_factor"] = pd.to_numeric(data["adj_factor"], errors="coerce")
    data = data.dropna(subset=["date", "symbol", "adj_factor"]).sort_values(
        ["symbol", "date"]
    )
    data["prev_factor"] = data.groupby("symbol")["adj_factor"].shift(1)
    data = data.dropna(subset=["prev_factor"])
    ratio = data["adj_factor"] / data["prev_factor"]
    changed = (ratio - 1.0).abs() > 1e-12
    if not changed.any():
        return pd.DataFrame(columns=columns)
    result = data.loc[changed, ["date", "symbol"]].copy()
    result["cash_div"] = 0.0
    result["share_ratio"] = ratio.loc[changed].to_numpy() - 1.0
    return result.reset_index(drop=True)
