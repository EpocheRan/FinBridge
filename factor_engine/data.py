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
        self.db = db or QuestDBClient(self.config, connect=False)

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

    def load_benchmark(
        self,
        code: str = "000300",
        start: str | date = "2020-01-01",
        end: str | date = "2100-01-01",
    ) -> pd.DataFrame:
        """基准指数日线（IDX 市场），用于 beta/corr。"""
        sql = (
            "SELECT ts, close FROM ohlcv_1d "
            "WHERE market = 'IDX' AND code = %s AND ts BETWEEN %s AND %s "
            "ORDER BY ts"
        )
        params = [
            str(code),
            datetime.combine(pd.Timestamp(start).date(), time.min),
            datetime.combine(pd.Timestamp(end).date(), time.max).replace(microsecond=0),
        ]
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts", "close"])
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        return frame.dropna(subset=["ts", "close"]).reset_index(drop=True)

    def load_reference(self, codes: list[str] | tuple[str, ...] | None) -> pd.DataFrame:
        """股本/市值参考：优先 fin_valuation_daily，其次 stock_basic_info。"""
        if not codes:
            return pd.DataFrame()
        placeholders = ",".join(["%s"] * len(codes))
        params = [str(code) for code in codes]
        try:
            frame = self.db.query_dataframe(
                "SELECT code, close, total_share, market_cap, float_market_cap "
                'FROM "fin_valuation_daily" '
                f"WHERE code IN ({placeholders}) LATEST ON ts PARTITION BY code",
                params,
            )
        except Exception as exc:
            logger.warning("读取 fin_valuation_daily 失败: %s", exc)
            frame = pd.DataFrame()

        if frame is not None and not frame.empty:
            frame = frame.copy()
            close = pd.to_numeric(frame.get("close"), errors="coerce")
            frame["total_share"] = pd.to_numeric(
                frame.get("total_share"), errors="coerce"
            )
            frame["total_mv"] = pd.to_numeric(
                frame.get("market_cap"), errors="coerce"
            )
            frame["float_mv"] = pd.to_numeric(
                frame.get("float_market_cap"), errors="coerce"
            )
            derived_total = frame["total_mv"].div(close)
            frame["total_share"] = frame["total_share"].fillna(derived_total)
            frame["float_share"] = frame["float_mv"].div(close)
            return frame[
                ["code", "total_share", "float_share", "total_mv", "float_mv"]
            ].reset_index(drop=True)

        try:
            frame = self.db.query_dataframe(
                "SELECT code, total_share, float_share, total_mv, float_mv "
                'FROM "stock_basic_info" '
                f"WHERE code IN ({placeholders}) LATEST ON ts PARTITION BY code",
                params,
            )
        except Exception as exc:
            logger.warning("读取 stock_basic_info 失败: %s", exc)
            return pd.DataFrame()
        if frame is None or frame.empty:
            return pd.DataFrame()
        return frame.reset_index(drop=True)

