"""上游数据版本（source_version）生成。"""

from __future__ import annotations

import hashlib

import pandas as pd


def _digest(values) -> str:
    try:
        array = pd.util.hash_pandas_object(pd.Series(values), index=False).to_numpy()
    except Exception:
        array = pd.Series(values).astype(str).to_numpy()
    return hashlib.sha256(array.tobytes()).hexdigest()[:12]


def price_source_version(frame: pd.DataFrame) -> str:
    """日线/复权版本：最大交易日 + 该日复权因子摘要（与查询窗口无关）。"""
    if frame is None or frame.empty or "ts" not in frame.columns:
        return "ohlcv_1d:empty"
    stamps = pd.to_datetime(frame["ts"], errors="coerce")
    max_ts = stamps.max()
    if pd.isna(max_ts):
        return "ohlcv_1d:empty"
    latest = frame[stamps == max_ts]
    basis = (
        latest["adj_factor"]
        if "adj_factor" in latest.columns
        else latest["close"]
    )
    return f"ohlcv_1d:{pd.Timestamp(max_ts).strftime('%Y%m%d')}:{_digest(basis)}"


def financial_source_version(report_ids) -> str:
    text = "|".join(sorted({str(item) for item in report_ids}))
    return f"fin:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def moneyflow_source_version(batch: str | None = None) -> str:
    return f"tushare.moneyflow.v1:{batch or 'na'}"
