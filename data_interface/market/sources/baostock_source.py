"""BaoStock 历史 K 线适配器。

- 只取**不复权**价（``adjustflag="3"``）；复权因子由 ``AdjFactorProvider`` 单独提供。
- ``turn`` 为换手率百分数，与项目统一口径一致。
- ``error_code != 0`` 必须暴露错误，不能当「无数据」。
"""

from __future__ import annotations

import logging

import pandas as pd

from data_interface.common.baostock_client import BaoStockClient
from data_interface.common.security_code import normalize_code

logger = logging.getLogger("DataInterface.BaoStockMarket")

_MINUTE_FREQ = {"5min": "5", "15min": "15", "30min": "30", "60min": "60"}
_DAILY_FREQ = {"d": "d", "w": "w", "m": "m"}


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
        from data_interface.common.security_code import to_baostock_code

        return to_baostock_code(code)

    def available(self, freq: str) -> bool:
        """BaoStock 不支持 1 分钟线。"""
        return freq != "1min"

    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        code = normalize_code(stock_code)
        if freq not in _MINUTE_FREQ and freq not in _DAILY_FREQ:
            logger.warning("BaoStock 不支持频率 %s", freq)
            return pd.DataFrame()

        full_code = self._to_full_code(code)
        if freq.endswith("min"):
            fields = "date,time,open,high,low,close,volume,amount"
        else:
            fields = "date,open,high,low,close,volume,amount,turn"

        try:
            result = self.client.client.query_history_k_data_plus(
                full_code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=_MINUTE_FREQ.get(freq) or _DAILY_FREQ[freq],
                adjustflag="3",  # 3 = 不复权
            )
        except Exception as exc:
            logger.warning("BaoStock 行情失败 %s: %s", code, exc)
            return pd.DataFrame()

        if result.error_code != "0":
            logger.warning("BaoStock 行情返回错误 %s: %s", code, result.error_msg)
            return pd.DataFrame()

        rows = []
        while result.next():
            rows.append(result.get_row_data())
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=result.fields)
        if freq.endswith("min"):
            frame["ts"] = pd.to_datetime(
                frame["time"].astype(str).str[:12], format="%Y%m%d%H%M", errors="coerce"
            )
            frame["code"] = code
            frame = frame.set_index("ts")
            out = frame[["code", "open", "high", "low", "close", "volume", "amount"]].copy()
            numeric = ["open", "high", "low", "close", "volume", "amount"]
        else:
            frame["ts"] = pd.to_datetime(frame["date"], errors="coerce")
            frame["code"] = code
            frame = frame.rename(columns={"turn": "turnover"})
            frame = frame.set_index("ts")
            out = frame[
                ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
            ].copy()
            numeric = ["open", "high", "low", "close", "volume", "amount", "turnover"]

        out[numeric] = out[numeric].apply(pd.to_numeric, errors="coerce")
        out = out.dropna(subset=["open", "high", "low", "close"]).sort_index()
        return out[~out.index.duplicated(keep="last")]
