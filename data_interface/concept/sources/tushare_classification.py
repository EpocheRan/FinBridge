"""Tushare 概念分类适配器（``concept``）。

只提供分类树（行业/概念/细分），不提供成分与行情：

- ``ts_code`` 如 ``800001.TS``；``class1/2/3`` 三级分类；
- ``item_size`` 为该分类下的样本数；
- 实测 3000 行，可作为概念板块的「分类字典」。

``ths_index`` / ``ths_member`` 需要 5000 积分，本适配器不使用。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

from ..models import SOURCE_TUSHARE_CONCEPT

logger = logging.getLogger("DataInterface.Concept.TushareConcept")

CANONICAL_COLUMNS = [
    "ts",
    "concept_code",
    "name",
    "class1_code",
    "class1_name",
    "class2_code",
    "class2_name",
    "class3_code",
    "class3_name",
    "item_size",
]


class TushareConceptClassificationSource:
    source_id = SOURCE_TUSHARE_CONCEPT

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

    def fetch_classification(self) -> pd.DataFrame:
        if self._tushare is not None and hasattr(self._tushare, "fetch_concept"):
            raw = pd.DataFrame(self._tushare.fetch_concept())
        else:
            raw = self.pro.concept()
        if raw is None or raw.empty:
            return pd.DataFrame(columns=CANONICAL_COLUMNS)
        frame = raw.loc[:, ~raw.columns.duplicated()].copy()
        if "ts_code" not in frame.columns:
            raise ValueError("Tushare concept 返回缺少 ts_code 列")

        if "trade_date" in frame.columns:
            frame["ts"] = pd.to_datetime(
                frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
            )
        else:
            frame["ts"] = pd.Timestamp(now_cn().date())
        frame["concept_code"] = (
            frame["ts_code"].astype(str).str.split(".").str[0].map(normalize_code)
        )
        frame["name"] = frame.get("name", frame["concept_code"]).astype(str).str.strip()
        for column in (
            "class1_code",
            "class1_name",
            "class2_code",
            "class2_name",
            "class3_code",
            "class3_name",
        ):
            if column not in frame.columns:
                frame[column] = None
        frame["item_size"] = pd.to_numeric(
            frame.get("item_size"), errors="coerce"
        ).fillna(0).astype("int64")

        frame = frame.dropna(subset=["ts", "concept_code"])
        frame = frame[frame["concept_code"].str.len() == 6]
        frame = (
            frame.drop_duplicates(subset=["ts", "concept_code"], keep="last")
            .sort_values(["ts", "concept_code"])
            .reset_index(drop=True)
        )
        return frame[CANONICAL_COLUMNS]
