"""复权换算。

存储契约（唯一真源）：

- ``ohlcv_*`` 表里 **只存不复权（raw）价**，外加一列 ``adj_factor``：
  **累计后复权因子**（Tushare ``adj_factor`` / BaoStock ``backAdjustFactor`` /
  AkShare 新浪 ``hfq_factor`` 的口径，单调、除权日跳变、不随新分红回溯变化）。
- 复权在**读取时**计算，因此任何一次分红都不需要回刷历史数据：
  - ``hfq``：``price * adj_factor``
  - ``qfq``：``price * adj_factor / anchor``，``anchor`` 为该标的**最新**因子，
    保证同一天在不同查询区间里返回同样的前复权价。

不变量：

- 同一次查询里所有行使用同一个 ``anchor``（否则 qfq 序列会在区间边界跳变）。
- 因子缺失（NULL）时**不做任何价格变换**，并在 ``frame.attrs`` 里标记
  ``adjust_fallback="missing_adj_factor"``，由上层决定是否提示用户。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger("DataInterface.Adjust")

ADJUST_NONE = "none"
ADJUST_QFQ = "qfq"
ADJUST_HFQ = "hfq"

VALID_ADJUST_MODES = (ADJUST_NONE, ADJUST_QFQ, ADJUST_HFQ)
DEFAULT_ADJUST = ADJUST_QFQ

_PRICE_COLUMNS = ("open", "high", "low", "close")
_FACTOR_COLUMN = "adj_factor"


def normalize_adjust(mode: str | None) -> str:
    """校验并规范化复权模式；未知值抛 ValueError（不要静默降级）。"""
    if mode is None or str(mode).strip() == "":
        return DEFAULT_ADJUST
    key = str(mode).strip().lower()
    aliases = {
        "": ADJUST_NONE,
        "none": ADJUST_NONE,
        "raw": ADJUST_NONE,
        "bfq": ADJUST_NONE,
        "不复权": ADJUST_NONE,
        "qfq": ADJUST_QFQ,
        "前复权": ADJUST_QFQ,
        "hfq": ADJUST_HFQ,
        "后复权": ADJUST_HFQ,
    }
    if key not in aliases:
        raise ValueError(
            f"不支持的复权模式: {mode}，可选值: {', '.join(VALID_ADJUST_MODES)}"
        )
    return aliases[key]


def _factor_series(frame: pd.DataFrame) -> pd.Series:
    if _FACTOR_COLUMN not in frame.columns:
        return pd.Series(index=frame.index, dtype="float64")
    return pd.to_numeric(frame[_FACTOR_COLUMN], errors="coerce")


def latest_factor(frame: pd.DataFrame) -> float | None:
    """取序列里最后一个有效因子（用作 qfq 锚点）。"""
    factors = _factor_series(frame).dropna()
    if factors.empty:
        return None
    return float(factors.iloc[-1])


def apply_adjust(
    frame: pd.DataFrame,
    mode: str = DEFAULT_ADJUST,
    *,
    anchor: float | None = None,
) -> pd.DataFrame:
    """按模式返回复权后的行情（不修改入参）。

    Args:
        frame: 至少包含 ``open/high/low/close``，可选 ``adj_factor``。
        mode: ``none`` / ``qfq`` / ``hfq``。
        anchor: qfq 锚点因子；缺省取本帧最后一个有效因子。
            **生产路径应传入数据库里的最新因子**，否则同一天在不同区间会
            得到不同的前复权价。
    """
    mode = normalize_adjust(mode)
    result = frame.copy()
    result.attrs.update(getattr(frame, "attrs", {}))
    result.attrs["adjust"] = mode

    if frame.empty:
        result.attrs.setdefault("adjust_fallback", None)
        return result

    factors = _factor_series(result)
    if factors.notna().sum() == 0:
        # 没有因子：保持原值（历史遗留数据/未提供因子的标的），显式标记
        result.attrs["adjust_fallback"] = "missing_adj_factor"
        if mode != ADJUST_NONE:
            logger.warning("行情缺少 adj_factor，按不复权返回（%s）", mode)
        return result

    # 原始收盘价留一份，供持仓盈亏等"按成本价（不复权）"的业务使用
    if mode != ADJUST_NONE and "close" in result.columns and "raw_close" not in result.columns:
        result["raw_close"] = pd.to_numeric(result["close"], errors="coerce")

    if mode == ADJUST_NONE:
        result.attrs["adjust_fallback"] = None
        return result

    if anchor is None or not pd.notna(anchor) or float(anchor) == 0.0:
        anchor = latest_factor(result)
        result.attrs["adjust_anchor_source"] = "frame"
    else:
        result.attrs["adjust_anchor_source"] = "database"
    if anchor is None:
        result.attrs["adjust_fallback"] = "missing_adj_factor"
        return result

    anchor = float(anchor)
    result.attrs["adjust_anchor"] = anchor

    if mode == ADJUST_QFQ:
        scale = factors / anchor
    else:  # hfq
        scale = factors

    # 因子缺失的行保持原值（不要用 NaN 污染整列）
    scale = scale.where(factors.notna(), 1.0).astype("float64")
    for column in _PRICE_COLUMNS:
        if column in result.columns:
            result[column] = (
                pd.to_numeric(result[column], errors="coerce") * scale
            ).round(4)

    result.attrs["adjust_fallback"] = None
    return result


def to_raw_price(adjusted_price: Any, adj_factor: Any, anchor: Any) -> Any:
    """把复权价还原成不复权价（持仓盈亏用）。"""
    try:
        if adj_factor in (None, 0) or anchor in (None, 0):
            return adjusted_price
        return float(adjusted_price) * float(anchor) / float(adj_factor)
    except (TypeError, ValueError):
        return adjusted_price


def frame_adjust_note(frame: pd.DataFrame) -> dict[str, Any]:
    """把 attrs 里的复权信息整理成可 JSON 化的字典。"""
    attrs = getattr(frame, "attrs", {}) or {}
    return {
        "adjust": attrs.get("adjust", ADJUST_NONE),
        "adjust_anchor": attrs.get("adjust_anchor"),
        "adjust_fallback": attrs.get("adjust_fallback"),
    }
