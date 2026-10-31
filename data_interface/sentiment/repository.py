"""情绪数据仓储（两融 / 龙虎榜 / 大宗交易）。"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time
from typing import Any, Iterable

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

logger = logging.getLogger("DataInterface.Sentiment.Repository")

#: 每张表的写入列（顺序与 DDL 一致）
COLUMNS: dict[str, tuple[str, ...]] = {
    "margin_detail": (
        "ts",
        "code",
        "rzye",
        "rqye",
        "rzmre",
        "rqyl",
        "rzche",
        "rqchl",
    ),
    "top_list": (
        "ts",
        "code",
        "name",
        "close",
        "pct_chg",
        "turnover_rate",
        "amount",
        "l_sell",
        "l_buy",
        "l_amount",
        "net_amount",
        "reason",
    ),
    "block_trade": (
        "ts",
        "code",
        "price",
        "vol",
        "amount",
        "buyer",
        "seller",
    ),
}

SENTIMENT_DATASETS = tuple(COLUMNS)


def _clean(value: Any) -> Any:
    """把 pandas / numpy 值转成 psycopg2 可绑定的原生值。"""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).to_pydatetime()
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _day_ts(day: str | date) -> datetime:
    return datetime.combine(pd.Timestamp(day).date(), time.min)


class SentimentRepository:
    def __init__(
        self,
        db: QuestDBClient | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config)

    def upsert(self, dataset: str, rows: Iterable[dict[str, Any]]) -> int:
        if dataset not in COLUMNS:
            raise ValueError(f"未知情绪数据集: {dataset!r}")
        columns = COLUMNS[dataset]
        payload = [
            tuple(_clean(row.get(column)) for column in columns)
            for row in rows
        ]
        if not payload:
            return 0
        quoted = ", ".join(f'"{column}"' for column in columns)
        sql = f'INSERT INTO "{dataset}" ({quoted}) VALUES %s'
        return int(self.db.execute_values(sql, payload, page_size=1000) or 0)

    def latest_ts(self, dataset: str) -> pd.Timestamp | None:
        if dataset not in COLUMNS:
            raise ValueError(f"未知情绪数据集: {dataset!r}")
        row = self.db.query(
            f'SELECT max(ts) FROM "{dataset}"', [], fetch_all=False
        )
        if not row or row[0] is None:
            return None
        return pd.Timestamp(row[0])

    def count_for_date(self, dataset: str, day: str | date) -> int:
        if dataset not in COLUMNS:
            raise ValueError(f"未知情绪数据集: {dataset!r}")
        row = self.db.query(
            f'SELECT count() FROM "{dataset}" WHERE ts = %s',
            [_day_ts(day)],
            fetch_all=False,
        )
        return int(row[0]) if row and row[0] is not None else 0
