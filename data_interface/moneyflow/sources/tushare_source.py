"""Tushare ``moneyflow`` 适配器。

口径（2026-10 实测）：

- 一次 ``trade_date`` 调用返回全市场约 5500 行，单次未超 6000 行上限；
- ``*_vol`` 单位是「手」，``*_amount`` 单位是「万元」；
- ``net_mf_amount`` 与四档净额不是同一口径，适配器只做字段搬运，不做合并。

入库前统一换算成「股 / 元」，并补齐 source / fetched_at。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from data_interface.common.security_code import normalize_code, to_ts_code
from data_interface.common.timeutil import now_cn

from ..models import BUCKETS, CANONICAL_COLUMNS, SOURCE_TUSHARE, TUSHARE_META

logger = logging.getLogger("DataInterface.Moneyflow.Tushare")

SOURCE_VERSION = "tushare.moneyflow.v1"
_LOT_TO_SHARES = 100
_WAN_TO_YUAN = 10_000


def _to_yyyymmdd(value: str | date | datetime | pd.Timestamp) -> str:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y%m%d")
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD，收到: {value!r}")
    return text


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


class TushareMoneyflowSource:
    """Tushare 个股资金流（四档）数据源。"""

    source_id = SOURCE_TUSHARE
    source_version = SOURCE_VERSION
    meta = TUSHARE_META

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        tushare: Any | None = None,
        pro: Any | None = None,
    ):
        self.config = config or {}
        self._tushare = tushare
        self._pro = pro

    @property
    def pro(self):
        if self._pro is None:
            from data_interface.financial.sources.tushare_source import (
                get_shared_tushare_source,
            )

            self._pro = get_shared_tushare_source(self.config).pro
        return self._pro

    def _fetch(self, **kwargs) -> pd.DataFrame:
        """优先走共享 ``TushareSource``（带限流/错误分类），否则直连 pro。"""
        if self._tushare is not None and hasattr(self._tushare, "fetch_moneyflow"):
            records = self._tushare.fetch_moneyflow(**kwargs)
            return pd.DataFrame(records)
        return self.pro.moneyflow(**kwargs)

    # ------------------------------------------------------------------ 拉取
    def fetch_by_date(self, trade_date: str | date | datetime) -> pd.DataFrame:
        raw = self._fetch(trade_date=_to_yyyymmdd(trade_date))
        return self._normalize(raw)

    def fetch_by_code(
        self,
        code: str,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
    ) -> pd.DataFrame:
        ts_code = to_ts_code(code)
        if not ts_code:
            raise ValueError(f"无效代码: {code!r}")
        raw = self._fetch(
            ts_code=ts_code,
            start_date=_to_yyyymmdd(start_date),
            end_date=_to_yyyymmdd(end_date),
        )
        return self._normalize(raw)

    # ------------------------------------------------------------------ 规范化
    def _normalize(self, raw: pd.DataFrame | None) -> pd.DataFrame:
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=CANONICAL_COLUMNS)

        frame = raw.copy()
        if "trade_date" not in frame.columns:
            raise ValueError("Tushare moneyflow 返回缺少 trade_date 列")

        frame["ts"] = pd.to_datetime(
            frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        frame["code"] = (
            frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        )
        frame["market"] = "MLCN"

        for bucket in BUCKETS:
            frame[f"buy_{bucket}_vol"] = self._shares(frame, f"buy_{bucket}_vol")
            frame[f"sell_{bucket}_vol"] = self._shares(frame, f"sell_{bucket}_vol")
            frame[f"buy_{bucket}_amount"] = self._yuan(frame, f"buy_{bucket}_amount")
            frame[f"sell_{bucket}_amount"] = self._yuan(frame, f"sell_{bucket}_amount")
        frame["net_mf_vol"] = self._shares(frame, "net_mf_vol")
        frame["net_mf_amount"] = self._yuan(frame, "net_mf_amount")

        frame["source"] = self.source_id
        frame["source_version"] = self.source_version
        frame["quality"] = "ok"
        frame["quality_note"] = None
        frame["fetched_at"] = now_cn()

        frame = frame.dropna(subset=["ts", "code"])
        frame = frame[frame["code"].astype(str).str.len() == 6]
        frame = (
            frame.drop_duplicates(subset=["ts", "code"], keep="last")
            .sort_values(["ts", "code"])
            .reset_index(drop=True)
        )
        return frame[CANONICAL_COLUMNS]

    @staticmethod
    def _shares(frame: pd.DataFrame, column: str) -> pd.Series:
        value = _numeric(frame, column) * _LOT_TO_SHARES
        return value.round().fillna(0).astype("int64")

    @staticmethod
    def _yuan(frame: pd.DataFrame, column: str) -> pd.Series:
        return (_numeric(frame, column) * _WAN_TO_YUAN).astype("float64")
