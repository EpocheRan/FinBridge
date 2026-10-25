"""指数/基准行情适配器（BaoStock 优先，AkShare 兜底）。"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger("DataInterface.IndexSource")


class IndexMarketSource:
    name = "index"

    def __init__(self, settings=None, timeout: int = 15):
        self.settings = settings
        self.timeout = timeout

    @staticmethod
    def _full_code(code: str) -> str:
        code = str(code).split(".")[0]
        if code.startswith(("000", "880", "9")):
            return f"sh.{code}"
        if code.startswith("399"):
            return f"sz.{code}"
        if code.startswith("899"):
            return f"bj.{code}"
        return f"sh.{code}"

    @staticmethod
    def _sina_code(code: str) -> str:
        code = str(code).split(".")[0]
        if code.startswith(("000", "880", "9")):
            return f"sh{code}"
        if code.startswith("399"):
            return f"sz{code}"
        return f"sh{code}"

    def fetch_ohlc(self, code: str, start: date, end: date) -> pd.DataFrame:
        # 顺序：BaoStock（免费、无积分） → Tushare（最及时、需积分） → AkShare。
        # 之前只有 baostock/akshare：baostock 的指数数据会滞后 1~2 个交易日，
        # akshare 走东财，在本机被代理拦截，导致 Beta 基准长期拉不到。
        for fetcher in (
            self._fetch_baostock,
            self._fetch_tushare,
            self._fetch_akshare,
        ):
            frame = fetcher(code, start, end)
            if frame is not None and not frame.empty:
                return frame
        return pd.DataFrame()

    def _fetch_tushare(self, code: str, start: date, end: date) -> pd.DataFrame:
        try:
            from data_interface.financial.sources.tushare_source import (
                get_shared_tushare_source,
            )

            rows = get_shared_tushare_source().fetch_index_daily(
                code, start_date=start.isoformat(), end_date=end.isoformat()
            )
            if not rows:
                return pd.DataFrame()
            frame = pd.DataFrame(rows)
            if "trade_date" not in frame.columns:
                return pd.DataFrame()
            frame["ts"] = pd.to_datetime(
                frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
            )
            for column in ("open", "high", "low", "close", "volume", "amount"):
                if column not in frame.columns:
                    frame[column] = 0.0
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna(subset=["ts", "close"])
            if frame.empty:
                return pd.DataFrame()
            frame["volume"] = frame["volume"].fillna(0)
            frame["amount"] = frame["amount"].fillna(0)
            frame["turnover"] = 0.0
            frame["code"] = str(code).split(".")[0]
            return (
                frame.set_index("ts")
                .sort_index()[
                    ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
                ]
            )
        except Exception as exc:
            logger.warning("Tushare 指数行情失败 %s: %s", code, exc)
            return pd.DataFrame()

    def _fetch_baostock(
        self, code: str, start: date, end: date
    ) -> pd.DataFrame:
        try:
            from data_interface.common.baostock_client import (
                get_shared_baostock_client,
            )

            client = get_shared_baostock_client()
            result = client.client.query_history_k_data_plus(
                self._full_code(code),
                "date,open,high,low,close,volume,amount",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            rows = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            if not rows:
                return pd.DataFrame()
            frame = pd.DataFrame(rows, columns=result.fields)
            frame["ts"] = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame.dropna(subset=["ts"]).set_index("ts")
            frame["code"] = str(code).split(".")[0]
            frame["turnover"] = 0.0
            for column in ("open", "high", "low", "close", "volume", "amount"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            return frame[
                ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
            ]
        except Exception as exc:
            logger.warning("BaoStock 指数行情失败 %s: %s", code, exc)
            return pd.DataFrame()

    def _fetch_akshare(self, code: str, start: date, end: date) -> pd.DataFrame:
        try:
            import akshare as ak

            frame = ak.index_zh_a_hist(
                symbol=str(code).split(".")[0],
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if frame is None or frame.empty:
                frame = ak.stock_zh_index_daily(symbol=self._sina_code(code))
            if frame is None or frame.empty:
                return pd.DataFrame()

            rename = {
                "日期": "ts",
                "date": "ts",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
            frame = frame.rename(
                columns={key: value for key, value in rename.items() if key in frame.columns}
            )
            if "ts" not in frame.columns:
                return pd.DataFrame()
            frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
            frame = frame.dropna(subset=["ts"])
            frame = frame[
                (frame["ts"].dt.date >= start) & (frame["ts"].dt.date <= end)
            ]
            frame = frame.set_index("ts")
            for column in ("open", "high", "low", "close", "volume", "amount"):
                if column not in frame.columns:
                    frame[column] = 0.0
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame["code"] = str(code).split(".")[0]
            frame["turnover"] = 0.0
            return frame[
                ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
            ]
        except Exception as exc:
            logger.warning("AkShare 指数行情失败 %s: %s", code, exc)
            return pd.DataFrame()
