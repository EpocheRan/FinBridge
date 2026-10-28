"""QuestDB 只读数据源。

回测运行期不回源；缺数据由同步任务预补齐。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Sequence

import pandas as pd

from backtest.domain.errors import DataError
from backtest.domain.models import DataBundle, SecurityStatus, TradingCalendar, Universe
from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.market.constants import normalize_frequency, ohlc_table_name

from .snapshot import make_snapshot

logger = logging.getLogger("Backtest.QuestDB")


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
        # 数据源构造不连接数据库：横截面/配置错误应在 load 之前失败，
        # 而不是先连一遍 QuestDB 再抛 BacktestError。
        self.db = db or QuestDBClient(self.config, connect=False)
        self.read_only = read_only

    def _load_security_status(self, codes: Sequence[str], end: date) -> SecurityStatus:
        placeholders = ",".join(["%s"] * len(codes))
        sql = (
            "SELECT code, last(is_st) AS is_st, last(list_date) AS list_date, "
            "last(delist_date) AS delist_date FROM stock_basic_info "
            f"WHERE code IN ({placeholders}) GROUP BY code"
        )
        try:
            frame = self.db.query_dataframe(sql, list(codes))
        except Exception as exc:
            logger.warning("证券状态读取失败（按近似处理）: %s", exc)
            return SecurityStatus(approximation=True)
        if frame is None or frame.empty:
            return SecurityStatus(approximation=True)
        current_st: dict[str, bool] = {}
        list_dates: dict[str, date] = {}
        delist_dates: dict[str, date] = {}
        for _, row in frame.iterrows():
            code = str(row.get("code"))
            value = row.get("is_st")
            current_st[code] = bool(value) if pd.notna(value) else False
            for column, target in (
                ("list_date", list_dates),
                ("delist_date", delist_dates),
            ):
                raw = row.get(column)
                if pd.notna(raw):
                    target[code] = pd.Timestamp(raw).date()
        # stock_basic_info 只有当前 is_st；历史 ST 需要 namechange 明细，暂标记近似。
        return SecurityStatus(
            current_st=current_st,
            list_dates=list_dates,
            delist_dates=delist_dates,
            approximation=True,
        )

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
            security_status=self._load_security_status(codes, end),
        )

    def load_factors(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        factor_ids: Sequence[str],
    ) -> pd.DataFrame:
        """读取因子长表，供 F.factor / factor_source=stored 使用。"""
        codes = tuple(sorted({str(item) for item in symbols}))
        ids = tuple(sorted({str(item) for item in factor_ids}))
        if not codes or not ids:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        code_placeholders = ",".join(["%s"] * len(codes))
        id_placeholders = ",".join(["%s"] * len(ids))
        sql = (
            "SELECT ts, code, factor_id, value FROM \"factor_value_daily\" "
            f"WHERE code IN ({code_placeholders}) "
            f"AND factor_id IN ({id_placeholders}) "
            "AND ts BETWEEN %s AND %s ORDER BY ts, code, factor_id"
        )
        params = [
            *codes,
            *ids,
            pd.Timestamp(start).normalize().to_pydatetime(),
            pd.Timestamp(end).normalize().to_pydatetime(),
        ]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:
            raise DataError(f"因子读取失败: {exc}") from exc
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        return frame.reset_index()

    def load_benchmark(
        self, code: str, start: date, end: date, *, freq: str = "d"
    ) -> pd.Series | None:
        if normalize_frequency(freq) != "d":
            return None
        sql = (
            "SELECT ts, close FROM ohlcv_1d "
            "WHERE market = 'IDX' AND code = %s AND ts >= %s AND ts <= %s "
            "ORDER BY ts"
        )
        params = [
            str(code),
            pd.Timestamp(start).normalize().to_pydatetime(),
            pd.Timestamp(end).normalize().to_pydatetime(),
        ]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception:
            return None
        if frame is None or frame.empty:
            return None
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        frame["date"] = pd.to_datetime(frame["ts"], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
        return pd.Series(
            pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
            index=pd.DatetimeIndex(frame["date"]),
            name=str(code),
        ).dropna()
