"""同花顺概念板块适配器（列表 + 板块日线）。

- 列表：``stock_board_concept_name_ths``，实测 375 个，返回 ``name/code``；
- 日线：``stock_board_concept_index_ths``，返回
  ``日期/开盘价/最高价/最低价/收盘价/成交量/成交额``，
  成交量单位「股」、成交额单位「元」。

成分不在本适配器范围内（AkShare 1.19.1 没有 ``stock_board_concept_cons_ths``）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

from ..models import BOARD_TYPE_CONCEPT, SOURCE_THS, Board

logger = logging.getLogger("DataInterface.Concept.THS")

_DAILY_RENAME = {
    "日期": "ts",
    "date": "ts",
    "开盘价": "open",
    "开盘": "open",
    "open": "open",
    "最高价": "high",
    "最高": "high",
    "high": "high",
    "最低价": "low",
    "最低": "low",
    "low": "low",
    "收盘价": "close",
    "收盘": "close",
    "close": "close",
    "成交量": "volume",
    "volume": "volume",
    "成交额": "amount",
    "amount": "amount",
}


def _ak():
    import akshare

    return akshare


class ThsConceptSource:
    source_id = SOURCE_THS

    def __init__(self, *, ak: Any | None = None):
        self._ak = ak

    @property
    def ak(self):
        if self._ak is None:
            self._ak = _ak()
        return self._ak

    # ------------------------------------------------------------------ 列表
    def list_boards(self) -> pd.DataFrame:
        raw = self.ak.stock_board_concept_name_ths()
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["board_code", "name", "board_type", "source", "source_code"])
        frame = raw.rename(
            columns={
                "概念名称": "name",
                "板块名称": "name",
                "概念代码": "board_code",
                "板块代码": "board_code",
                "code": "board_code",
            }
        )
        if "name" not in frame.columns or "board_code" not in frame.columns:
            logger.warning("同花顺板块列表列名无法识别: %s", list(frame.columns)[:8])
            return pd.DataFrame(columns=["board_code", "name", "board_type", "source", "source_code"])
        frame = frame[["board_code", "name"]].copy()
        frame["board_code"] = (
            frame["board_code"].astype(str).str.strip().str.zfill(6)
        )
        frame["name"] = frame["name"].astype(str).str.strip()
        frame = frame[
            frame["board_code"].str.fullmatch(r"\d{6}")
            & frame["name"].ne("")
        ]
        frame["board_type"] = BOARD_TYPE_CONCEPT
        frame["source"] = self.source_id
        frame["source_code"] = frame["board_code"]
        frame = frame.drop_duplicates(subset=["board_code"], keep="last")
        return frame.sort_values("board_code").reset_index(drop=True)

    # ------------------------------------------------------------------ 日线
    def fetch_board_daily(
        self, board: Board, start_date: str | date, end_date: str | date
    ) -> pd.DataFrame:
        raw = self.ak.stock_board_concept_index_ths(
            symbol=board.name,
            start_date=str(start_date)[:10].replace("-", ""),
            end_date=str(end_date)[:10].replace("-", ""),
        )
        if raw is None or raw.empty:
            return pd.DataFrame(
                columns=["ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
            )
        frame = raw.rename(columns=_DAILY_RENAME).copy()
        if "ts" not in frame.columns or "close" not in frame.columns:
            logger.warning(
                "板块日线列名无法识别 %s: %s", board.name, list(raw.columns)[:10]
            )
            return pd.DataFrame(
                columns=["ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
            )
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column not in frame.columns:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["high"] = frame["high"].fillna(frame["close"])
        frame["low"] = frame["low"].fillna(frame["close"])
        frame["open"] = frame["open"].fillna(frame["close"])
        frame["volume"] = frame["volume"].fillna(0).round().astype("int64")
        frame = frame.dropna(subset=["ts", "close"])
        frame = frame.sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
        frame["pct_chg"] = frame["close"].pct_change()
        return frame[
            ["ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        ].reset_index(drop=True)
