"""Tushare 股票行情适配器（仅日/周/月线，不复权）。

Tushare 作为可选行情源接入，主要价值是：
- 复用已有的 Tushare token / 限流器 / 错误分类；
- 在 BaoStock/AkShare 同时不可用时提供第三条日/周/月线路径；
- 统一返回 canonical 列，复权因子仍由 ``AdjFactorProvider`` 单独贴。

分钟线不在本适配器范围内（Tushare ``stk_mins`` 需要更高积分）。
"""

from __future__ import annotations

import logging

import pandas as pd

from data_interface.common.security_code import normalize_code

logger = logging.getLogger("DataInterface.TushareMarket")

PRICE_FREQUENCIES = ("d", "w", "m")


class TushareMarketSource:
    name = "tushare"

    def __init__(self, config: dict | None = None, *, tushare=None):
        self.config = config or {}
        self._tushare = tushare

    @property
    def tushare(self):
        if self._tushare is None:
            # 复用进程内共享实例：否则每个 MarketDataFetcher 都会新建一套
            # Tushare 限流器/配额账本
            from data_interface.financial.sources.tushare_source import (
                get_shared_tushare_source,
            )

            self._tushare = get_shared_tushare_source(self.config)
        return self._tushare

    def available(self, freq: str) -> bool:
        return freq in PRICE_FREQUENCIES

    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        code = normalize_code(stock_code)
        if not code or freq not in PRICE_FREQUENCIES:
            logger.warning("Tushare 行情不支持 %s / %s", code, freq)
            return pd.DataFrame()
        try:
            rows = self.tushare.fetch_price_history(
                code, start_date=start_date, end_date=end_date, freq=freq
            )
        except Exception as exc:
            logger.warning("Tushare 行情失败 %s(%s): %s", code, freq, exc)
            return pd.DataFrame()
        if not rows:
            return pd.DataFrame()
        return self._standardize(rows, code, freq)

    @staticmethod
    def _standardize(rows: list[dict], code: str, freq: str = "d") -> pd.DataFrame:
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame["ts"] = pd.to_datetime(
            frame.get("trade_date").astype(str), format="%Y%m%d", errors="coerce"
        )
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame.get(column), errors="coerce")

        # 单位换算只对日线成立：
        #   daily  : vol = 手, amount = 千元
        #   weekly : vol = 股, amount = 元（实测等于日线合计 ×100 / ×1000）
        #   monthly: 同 weekly
        # 早期版本对周/月线也乘了 100/1000，导致库内的周/月线量额被放大。
        raw_volume = pd.to_numeric(frame.get("vol"), errors="coerce")
        raw_amount = pd.to_numeric(frame.get("amount"), errors="coerce")
        if freq == "d":
            volume = raw_volume.fillna(0) * 100
            amount = raw_amount.fillna(0) * 1000
        else:
            volume = raw_volume.fillna(0)
            amount = raw_amount.fillna(0)
        frame["volume"] = volume.round().astype("int64")
        frame["amount"] = amount.astype("float64")
        frame["turnover"] = pd.NA
        frame["code"] = code
        frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
        frame = frame.set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame[
            ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
        ]
