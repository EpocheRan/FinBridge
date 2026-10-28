"""东方财富概念板块适配器（列表 + 成分）。

本机网络对 ``push2.eastmoney.com`` 连接被断，因此仅作可选源：
能连通时优先用它拿成分（一次调用一个板块），否则回退到同花顺网页。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from data_interface.common.security_code import normalize_code

from ..models import SOURCE_EM, Board

logger = logging.getLogger("DataInterface.Concept.EM")


class EmConceptSource:
    source_id = SOURCE_EM

    def __init__(self, *, ak: Any | None = None):
        self._ak = ak

    @property
    def ak(self):
        if self._ak is None:
            import akshare

            self._ak = akshare
        return self._ak

    def list_boards(self) -> pd.DataFrame:
        raw = self.ak.stock_board_concept_name_em()
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["board_code", "name", "source", "source_code"])
        frame = raw.rename(
            columns={
                "板块代码": "board_code",
                "板块名称": "name",
                "代码": "board_code",
                "名称": "name",
            }
        )
        if "board_code" not in frame.columns or "name" not in frame.columns:
            logger.warning("东财板块列表列名无法识别: %s", list(raw.columns)[:10])
            return pd.DataFrame(columns=["board_code", "name", "source", "source_code"])
        frame = frame[["board_code", "name"]].copy()
        frame["board_code"] = frame["board_code"].astype(str).str.strip()
        frame["name"] = frame["name"].astype(str).str.strip()
        frame["source"] = self.source_id
        frame["source_code"] = frame["board_code"]
        return frame.drop_duplicates(subset=["board_code"], keep="last").reset_index(drop=True)

    def fetch_members(self, board: Board) -> pd.DataFrame:
        raw = self.ak.stock_board_concept_cons_em(symbol=board.name)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=["stock_code", "stock_name"])
        frame = raw.rename(columns={"代码": "stock_code", "名称": "stock_name"})
        if "stock_code" not in frame.columns or "stock_name" not in frame.columns:
            logger.warning("东财成分列名无法识别 %s: %s", board.name, list(raw.columns)[:10])
            return pd.DataFrame(columns=["stock_code", "stock_name"])
        frame = frame[["stock_code", "stock_name"]].copy()
        frame["stock_code"] = frame["stock_code"].astype(str).map(normalize_code)
        frame["stock_name"] = frame["stock_name"].astype(str).str.strip()
        frame = frame[frame["stock_code"].str.len() == 6]
        frame = frame.drop_duplicates(subset=["stock_code"], keep="last").reset_index(drop=True)
        frame.attrs.update({"partial": False, "source": self.source_id})
        return frame
