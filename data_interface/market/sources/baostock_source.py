from __future__ import annotations

import logging

import pandas as pd

from data_interface.common.baostock_client import BaoStockClient

logger = logging.getLogger("DataInterface.BaoStockMarket")


class BaoStockMarketSource:
    """BaoStock 历史 K 线适配器。"""

    name = "baostock"

    def __init__(self, client: BaoStockClient | None = None):
        self._client = client

    @property
    def client(self) -> BaoStockClient:
        if self._client is None:
            self._client = BaoStockClient()
        return self._client

    @staticmethod
    def _to_full_code(code: str) -> str:
        if code.startswith(("60", "68", "51", "58", "50")):
            return f"sh.{code}"
        if code.startswith(("4", "8")):
            return f"bj.{code}"
        return f"sz.{code}"

    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        full_code = self._to_full_code(stock_code)
        frequency_map = {
            "5min": "5",
            "15min": "15",
            "30min": "30",
            "60min": "60",
            "d": "d",
            "w": "w",
            "m": "m",
        }
        if freq.endswith("min"):
            fields = "date,time,open,high,low,close,volume,amount"
        else:
            fields = "date,open,high,low,close,volume,amount,turn"

        try:
            rs = self.client.client.query_history_k_data_plus(
                full_code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency_map[freq],
                adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=rs.fields)
            if freq.endswith("min"):
                df["ts"] = pd.to_datetime(df["time"].str[:12])
                df["code"] = stock_code
                df.set_index("ts", inplace=True)
                out = df[["code", "open", "high", "low", "close", "volume", "amount"]].copy()
                numeric = ["open", "high", "low", "close", "volume", "amount"]
            else:
                df["ts"] = pd.to_datetime(df["date"])
                df["code"] = stock_code
                df.rename(columns={"turn": "turnover"}, inplace=True)
                df.set_index("ts", inplace=True)
                out = df[
                    ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
                ].copy()
                numeric = ["open", "high", "low", "close", "volume", "amount", "turnover"]
            out[numeric] = out[numeric].apply(pd.to_numeric, errors="coerce")
            return out
        except Exception as exc:
            logger.warning("BaoStock 行情失败 %s: %s", stock_code, exc)
            return pd.DataFrame()
