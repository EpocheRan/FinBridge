"""QuestDB 只读数据源。

回测运行期不回源；缺数据由同步任务预补齐。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Sequence

import pandas as pd

from backtest.domain.errors import DataError
from backtest.domain.models import DataBundle, TradingCalendar, Universe
from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.market.constants import normalize_frequency, ohlc_table_name

from .snapshot import make_snapshot


def _calendar(frame: pd.DataFrame) -> TradingCalendar:
    dates = pd.to_datetime(frame["date"]).dropna().dt.normalize().unique()
    return TradingCalendar(tuple(sorted(pd.Timestamp(item) for item in dates)))


class QuestDBDataSource:
    name = "questdb"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        db: QuestDBClient | None = None,
        read_only: bool = True,
    ):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)
        self.read_only = read_only

    def load(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        warmup: int = 0,
        freq: str = "d",
    ) -> DataBundle:
        codes = tuple(sorted(set(str(item) for item in symbols)))
        if not codes:
            raise DataError("QuestDBDataSource 需要至少一个 symbol")
        normalized = normalize_frequency(freq)
        table = ohlc_table_name(normalized)
        start_ts = pd.Timestamp(start).normalize() - pd.Timedelta(days=max(0, int(warmup)) * 2 + 10)
        end_ts = pd.Timestamp(end).normalize()
        placeholders = ",".join(["%s"] * len(codes))
        sql = (
            "SELECT ts, code AS symbol, open, high, low, close, volume, amount, adj_factor "
            f"FROM {table} "
            f"WHERE code IN ({placeholders}) AND ts >= %s AND ts <= %s "
            "ORDER BY ts, code"
        )
        params = [*codes, start_ts.to_pydatetime(), end_ts.to_pydatetime()]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:
            raise DataError(f"QuestDB 读取失败: {exc}") from exc
        if frame is None or frame.empty:
            raise DataError(f"QuestDB 无数据: {codes} {start}..{end} {normalized}")
        frame = frame.reset_index()
        if "ts" not in frame.columns:
            frame = frame.rename(columns={frame.columns[0]: "ts"})
        frame["date"] = pd.to_datetime(frame["ts"], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
        frame = frame.drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True)

        factor = pd.to_numeric(frame.get("adj_factor"), errors="coerce")
        fallback = factor.isna()
        frame["adjust_fallback"] = fallback
        frame["adj_factor"] = factor.fillna(1.0)
        frame["hfq_close"] = frame["close"] * frame["adj_factor"]
        anchors = frame.groupby("symbol")["adj_factor"].last().astype(float)
        anchor_map = frame["symbol"].map(anchors).replace(0, 1.0).fillna(1.0)
        frame["adj_close"] = frame["hfq_close"] / anchor_map
        frame = frame[
            [
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "adj_factor",
                "hfq_close",
                "adj_close",
                "adjust_fallback",
            ]
        ].reset_index(drop=True)

        universe = Universe.from_codes(
            codes,
            as_of=pd.Timestamp(start).date(),
            fingerprint=hashlib.sha256("|".join(codes).encode()).hexdigest()[:16],
        )
        return DataBundle(
            frame=frame,
            calendar=_calendar(frame),
            snapshot=make_snapshot(self.name, frame),
            universe=universe,
        )
