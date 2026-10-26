"""QuestDB/Pandas 数据源适配器（PyBroker DataSource 插槽）。

设计口径：
- ``close`` 使用不复权价，用于成交、整手和盈亏；
- ``adj_close`` 使用前复权价，用于指标/信号，避免除权跳空；
- 回测前应先把数据同步进 QuestDB，运行期只读，避免在 ``_fetch_data`` 里回源。

用法::

    from data_interface.market.loader import MarketDataLoader
    from backtest.data_questdb import QuestDBDataSource
    from pybroker import Strategy

    loader = MarketDataLoader()
    source = QuestDBDataSource(loader)
    strategy = Strategy(source, "2020-01-01", "2025-12-31")
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from pybroker.data import DataSource

from data_interface.common.security_code import normalize_code

_REQUIRED_COLUMNS = ["symbol", "date", "open", "high", "low", "close"]
_OPTIONAL_COLUMNS = ["volume", "adj_close"]


def _ensure_date(frame: pd.DataFrame) -> pd.DataFrame:
    """把加载器返回的 ``ts``/索引统一成 ``date`` 列。"""
    result = frame.copy()
    if "date" in result.columns:
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        return result
    if "ts" in result.columns:
        result = result.rename(columns={"ts": "date"})
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        return result

    result = result.reset_index()
    first = result.columns[0]
    if first == "ts":
        result = result.rename(columns={"ts": "date"})
    elif first != "date":
        result = result.rename(columns={first: "date"})
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    return result


class QuestDBDataSource(DataSource):
    """把 ``MarketDataLoader.ohlc`` 包装成 PyBroker 的 ``DataSource``。"""

    def __init__(
        self,
        loader,
        *,
        freq: str = "d",
        adjust: str = "none",
        signal_adjust: str = "qfq",
    ):
        super().__init__()
        self.loader = loader
        self.freq = freq
        self.adjust = adjust
        self.signal_adjust = signal_adjust
        # PyBroker 允许 DataSource 注册额外的列，指标函数可以读取 data.adj_close。
        self._scope.register_custom_cols("adj_close")

    def _fetch_data(
        self,
        symbols: frozenset[str],
        start_date: datetime,
        end_date: datetime,
        timeframe: str | None,
        adjust,
    ) -> pd.DataFrame:
        codes = sorted(str(symbol) for symbol in symbols)
        start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        end = pd.Timestamp(end_date).strftime("%Y-%m-%d")

        raw = self.loader.ohlc(
            codes, start, end, freq=self.freq, adjust=self.adjust
        )
        signal = self.loader.ohlc(
            codes, start, end, freq=self.freq, adjust=self.signal_adjust
        )

        frames: list[pd.DataFrame] = []
        for symbol in codes:
            code = normalize_code(symbol) or symbol
            frame = raw.get(code)
            if frame is None:
                frame = raw.get(symbol)
            if frame is None or frame.empty:
                continue

            frame = _ensure_date(frame).drop_duplicates("date", keep="last")
            frame["symbol"] = symbol

            signal_frame = signal.get(code)
            if signal_frame is None:
                signal_frame = signal.get(symbol)
            if signal_frame is not None and not signal_frame.empty:
                signal_frame = _ensure_date(signal_frame).drop_duplicates(
                    "date", keep="last"
                )
                adj_close = (
                    signal_frame.set_index("date")["close"]
                    .reindex(frame["date"])
                    .to_numpy()
                )
            else:
                adj_close = frame["close"].to_numpy()

            frame["adj_close"] = adj_close
            for column in ("open", "high", "low", "close"):
                if column not in frame.columns:
                    frame[column] = float("nan")
            if "volume" not in frame.columns:
                if "vol" in frame.columns:
                    frame["volume"] = frame["vol"]
                else:
                    frame["volume"] = float("nan")

            frames.append(
                frame[_REQUIRED_COLUMNS + _OPTIONAL_COLUMNS].dropna(
                    subset=["date"]
                )
            )

        if not frames:
            return pd.DataFrame(columns=_REQUIRED_COLUMNS + _OPTIONAL_COLUMNS)

        return (
            pd.concat(frames, ignore_index=True)
            .sort_values(["date", "symbol"])
            .reset_index(drop=True)
        )
