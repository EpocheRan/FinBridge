"""价格数据加载（factor_engine 侧的唯一 raw 读取入口）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, time

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

logger = logging.getLogger("FactorEngine.Data")


class PriceData:
    def __init__(self, db: QuestDBClient | None = None, config: dict | None = None):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)

    def load(
        self,
        codes: list[str] | tuple[str, ...] | None,
        start: str | date,
        end: str | date,
    ) -> pd.DataFrame:
        start_ts = datetime.combine(pd.Timestamp(start).date(), time.min)
        end_ts = datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0)
        sql = (
            "SELECT ts, code, open, high, low, close, volume, amount, turnover, adj_factor "
            'FROM "ohlcv_1d" WHERE ts BETWEEN %s AND %s'
        )
        params: list[object] = [start_ts, end_ts]
        if codes:
            placeholders = ",".join(["%s"] * len(codes))
            sql += f" AND code IN ({placeholders})"
            params.extend(str(code) for code in codes)
        sql += " ORDER BY ts, code"
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        factor = frame["adj_factor"].fillna(1.0) if "adj_factor" in frame.columns else 1.0
        frame["hfq_close"] = frame["close"] * factor
        frame["hfq_high"] = frame["high"] * factor
        frame["hfq_low"] = frame["low"] * factor
        frame["hfq_open"] = frame["open"] * factor
        return frame.dropna(subset=["ts", "code", "close"]).reset_index(drop=True)
