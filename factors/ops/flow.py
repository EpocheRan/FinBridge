"""情绪/资金类因子算子：两融（margin）与资金流（moneyflow）。

数据列约定
----------
回测/研究的面板帧里，这些列由数据源在 ``(symbol, date)`` 上左连接得到：

- 两融（``margin_detail``）：``margin_rzye`` / ``margin_rzmre`` / ``margin_rqye`` / ``margin_rqyl``
- 资金流（``moneyflow_daily``）：``mf_buy_sm`` / ``mf_sell_sm`` / ``mf_buy_lg`` /
  ``mf_sell_lg`` / ``mf_buy_elg`` / ``mf_sell_elg`` / ``mf_net_amount``

列缺失时返回全 NaN，而不是 KeyError——这样非两融标的、或没接该数据源的回测
会自然降级成「无信号」，而不是把整条策略打挂。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..library import factor

_EPS = 1e-12


def _col(frame: pd.DataFrame, name: str) -> pd.Series:
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce").astype("float64")
    return pd.Series(np.nan, index=frame.index, dtype="float64")


def _amount(frame: pd.DataFrame) -> pd.Series:
    return _col(frame, "amount").replace(0, np.nan)


# ---------------------------------------------------------------- 两融
@factor(
    "sentiment.margin_balance_chg_20d",
    unit="ratio",
    window=20,
    inputs=("margin_rzye",),
)
def margin_balance_chg_20d(frame: pd.DataFrame) -> pd.Series:
    """融资余额 20 日变化率：杠杆盘加仓速度（拥挤度代理，方向 -1）。"""
    rzye = _col(frame, "margin_rzye")
    return rzye / rzye.shift(20).replace(0, np.nan) - 1


@factor(
    "sentiment.margin_short_ratio",
    unit="ratio",
    window=1,
    inputs=("margin_rqye", "margin_rzye"),
)
def margin_short_ratio(frame: pd.DataFrame) -> pd.Series:
    """融券余额 / 融资余额：看空力量占杠杆盘比重（方向 -1）。"""
    return _col(frame, "margin_rqye") / _col(frame, "margin_rzye").replace(0, np.nan)


@factor(
    "sentiment.margin_buy_ratio_20d",
    unit="ratio",
    window=20,
    inputs=("margin_rzmre", "amount"),
)
def margin_buy_ratio_20d(frame: pd.DataFrame) -> pd.Series:
    """MA20(融资买入额 / 成交额)：杠杆参与度（方向 -1）。"""
    ratio = _col(frame, "margin_rzmre") / _amount(frame)
    return ratio.rolling(20, min_periods=10).mean()


# ---------------------------------------------------------------- 资金流
@factor(
    "sentiment.flow_large_net_ratio_20d",
    unit="ratio",
    window=20,
    inputs=("mf_buy_lg", "mf_buy_elg", "mf_sell_lg", "mf_sell_elg", "amount"),
)
def flow_large_net_ratio_20d(frame: pd.DataFrame) -> pd.Series:
    """MA20((大单+特大单净买入) / 成交额)：机构方向（方向 +1）。"""
    buy = _col(frame, "mf_buy_lg") + _col(frame, "mf_buy_elg")
    sell = _col(frame, "mf_sell_lg") + _col(frame, "mf_sell_elg")
    ratio = (buy - sell) / _amount(frame)
    return ratio.rolling(20, min_periods=10).mean()


@factor(
    "sentiment.flow_retail_net_ratio_20d",
    unit="ratio",
    window=20,
    inputs=("mf_buy_sm", "mf_sell_sm", "amount"),
)
def flow_retail_net_ratio_20d(frame: pd.DataFrame) -> pd.Series:
    """MA20((小单净买入) / 成交额)：散户接盘（方向 -1）。"""
    ratio = (_col(frame, "mf_buy_sm") - _col(frame, "mf_sell_sm")) / _amount(frame)
    return ratio.rolling(20, min_periods=10).mean()


#: 因子 id -> (函数, 窗口, 单位, 方向, 数据集)
FLOW_FACTORS: dict[str, tuple] = {
    "sentiment.margin_balance_chg_20d": (
        margin_balance_chg_20d, 20, "ratio", -1, "margin_detail",
    ),
    "sentiment.margin_short_ratio": (
        margin_short_ratio, 1, "ratio", -1, "margin_detail",
    ),
    "sentiment.margin_buy_ratio_20d": (
        margin_buy_ratio_20d, 20, "ratio", -1, "margin_detail",
    ),
    "sentiment.flow_large_net_ratio_20d": (
        flow_large_net_ratio_20d, 20, "ratio", 1, "moneyflow_daily",
    ),
    "sentiment.flow_retail_net_ratio_20d": (
        flow_retail_net_ratio_20d, 20, "ratio", -1, "moneyflow_daily",
    ),
}


def lookup(name: str) -> tuple:
    """按 factor_id 查 (fn, window, unit, direction, dataset)。"""
    try:
        return FLOW_FACTORS[name]
    except KeyError as exc:
        raise KeyError(name) from exc


__all__ = [
    "FLOW_FACTORS",
    "flow_large_net_ratio_20d",
    "flow_retail_net_ratio_20d",
    "lookup",
    "margin_balance_chg_20d",
    "margin_buy_ratio_20d",
    "margin_short_ratio",
]
