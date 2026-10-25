"""日频因子服务：取数 → FactorEngine → 写库。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Sequence

import pandas as pd

from screener.domain.clock import now_local
from screener.domain.factor_engine import FactorEngine
from screener.application.models import ComputeResult
from screener.application.ports import FactorStorePort, MarketDataPort

logger = logging.getLogger("Screener.FactorService")


class FactorService:
    def __init__(
        self,
        market: MarketDataPort,
        store: FactorStorePort,
        engine: FactorEngine,
    ):
        self.market = market
        self.store = store
        self.engine = engine

    def compute_chunk(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        write_start: date | None = None,
    ) -> ComputeResult:
        codes = list(codes)
        if not codes:
            return ComputeResult(rows_written=0, frame=pd.DataFrame())

        raw = self.market.read_ohlc(codes, start, end, "d")
        if raw.empty:
            return ComputeResult(rows_written=0, frame=pd.DataFrame())

        benchmark = self.market.read_benchmark(start, end)
        reference = self.market.read_reference(codes)
        factors = self.engine.compute_daily(raw, benchmark, reference)
        if factors.empty:
            return ComputeResult(rows_written=0, frame=factors)

        factors = factors.copy()
        factors["market"] = "MLCN"
        factors["formula_version"] = self.engine.config.formula_version
        factors["computed_at"] = now_local()
        if write_start is not None:
            factors = factors[
                pd.to_datetime(factors["ts"]).dt.date >= write_start
            ]
        if factors.empty:
            return ComputeResult(rows_written=0, frame=factors)

        written = self.store.upsert_daily(factors)
        visible = self.store.wait_daily_visible(
            codes, write_start or start, end
        )
        if not visible:
            logger.warning("因子写入后暂未对查询可见: %s", list(codes)[:3])
        return ComputeResult(rows_written=written, frame=factors)

    def coverage(
        self, codes: Sequence[str], start: date, end: date
    ) -> pd.DataFrame:
        return self.store.coverage(list(codes), start, end)
