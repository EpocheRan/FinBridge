"""数据快照哈希。"""

from __future__ import annotations

import hashlib
from typing import Mapping

import pandas as pd

from backtest.domain.models import DataSnapshot


def make_snapshot(source: str, frame: pd.DataFrame) -> DataSnapshot:
    anchors = (
        frame.groupby("symbol")["adj_factor"].last().astype(float).to_dict()
        if "adj_factor" in frame.columns
        else {}
    )
    max_ts = pd.to_datetime(frame["date"]).max()
    columns = [
        column
        for column in ("symbol", "date", "close", "adj_factor", "volume")
        if column in frame.columns
    ]
    if columns:
        rows_hash = pd.util.hash_pandas_object(
            frame[columns].reset_index(drop=True), index=False
        ).to_numpy(dtype="uint64")
        content_hash = hashlib.sha256(rows_hash.tobytes()).hexdigest()[:16]
    else:
        content_hash = "empty"
    payload = (
        f"{source}|{max_ts}|{len(frame)}|{content_hash}|"
        f"{sorted(anchors.items())}"
    )
    return DataSnapshot(
        source=source,
        max_ts=max_ts,
        rows=len(frame),
        anchor_by_symbol=anchors,
        hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    )
