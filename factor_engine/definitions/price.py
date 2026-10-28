"""内置价格类因子定义（M2 首批）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from factors.ops import atr_wilder, pct_change, realized_volatility
from factors.spec import Dependency, FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category


@dataclass(frozen=True)
class BuiltinFactor:
    spec: FactorSpec
    compute: Callable[[pd.DataFrame], pd.Series]


def _momentum(df: pd.DataFrame) -> pd.Series:
    return pct_change(df["hfq_close"], 20)


def _reversal(df: pd.DataFrame) -> pd.Series:
    return -pct_change(df["hfq_close"], 5)


def _atr(df: pd.DataFrame) -> pd.Series:
    return atr_wilder(df["hfq_high"], df["hfq_low"], df["hfq_close"], 14)


def _volatility(df: pd.DataFrame) -> pd.Series:
    return realized_volatility(pct_change(df["hfq_close"]), 20)


def _turnover(df: pd.DataFrame) -> pd.Series:
    return df["turnover"]


PRICE_FACTORS: tuple[BuiltinFactor, ...] = (
    BuiltinFactor(
        spec=FactorSpec(
            factor_id="momentum.ret",
            name="20日动量",
            category=Category.MOMENTUM,
            subcategory="return",
            source=SourceSpec("ohlcv_1d", "questdb", ("close", "adj_factor"), "trade_date"),
            window=20,
            freq="1d",
            value_kind="daily",
            update_mode="shift",
            unit="ratio",
            direction="positive",
            price_basis="hfq",
            version="v1",
            source_version_policy="ohlcv_1d",
            dependencies=(Dependency("raw", "hfq_close"),),
            warmup=20,
            formula="pct_change(hfq_close, 20)",
            aliases=("mom20",),
            materialization=Materialization.AUTO,
        ),
        compute=_momentum,
    ),
    BuiltinFactor(
        spec=FactorSpec(
            factor_id="reversal.short",
            name="5日反转",
            category=Category.REVERSAL,
            subcategory="short",
            source=SourceSpec("ohlcv_1d", "questdb", ("close", "adj_factor"), "trade_date"),
            window=5,
            freq="1d",
            value_kind="daily",
            update_mode="shift",
            unit="ratio",
            direction="negative",
            price_basis="hfq",
            version="v1",
            source_version_policy="ohlcv_1d",
            dependencies=(Dependency("raw", "hfq_close"),),
            warmup=5,
            formula="-pct_change(hfq_close, 5)",
            aliases=("rev5",),
            materialization=Materialization.AUTO,
        ),
        compute=_reversal,
    ),
    BuiltinFactor(
        spec=FactorSpec(
            factor_id="technical.atr",
            name="ATR14",
            category=Category.TECHNICAL,
            subcategory="atr",
            source=SourceSpec("ohlcv_1d", "questdb", ("high", "low", "close", "adj_factor"), "trade_date"),
            window=14,
            freq="1d",
            value_kind="daily",
            update_mode="recursive",
            unit="price_delta",
            direction="neutral",
            price_basis="hfq",
            version="v1",
            source_version_policy="ohlcv_1d",
            dependencies=(Dependency("raw", "hfq_high"), Dependency("raw", "hfq_low"), Dependency("raw", "hfq_close")),
            warmup=14,
            formula="atr_wilder(hfq_high, hfq_low, hfq_close, 14)",
            aliases=("atr14",),
            materialization=Materialization.AUTO,
        ),
        compute=_atr,
    ),
    BuiltinFactor(
        spec=FactorSpec(
            factor_id="risk.vol",
            name="20日年化波动率",
            category=Category.RISK,
            subcategory="total_vol",
            source=SourceSpec("ohlcv_1d", "questdb", ("close", "adj_factor"), "trade_date"),
            window=20,
            freq="1d",
            value_kind="daily",
            update_mode="window",
            unit="ratio",
            direction="negative",
            price_basis="hfq",
            version="v1",
            source_version_policy="ohlcv_1d",
            dependencies=(Dependency("raw", "hfq_close"),),
            warmup=20,
            formula="realized_volatility(pct_change(hfq_close,1), 20)",
            aliases=("vol20",),
            materialization=Materialization.AUTO,
        ),
        compute=_volatility,
    ),
    BuiltinFactor(
        spec=FactorSpec(
            factor_id="liquidity.turnover",
            name="换手率",
            category=Category.LIQUIDITY,
            subcategory="turnover",
            source=SourceSpec("ohlcv_1d", "questdb", ("turnover",), "trade_date"),
            window=1,
            freq="1d",
            value_kind="daily",
            update_mode="shift",
            unit="pct",
            direction="neutral",
            price_basis="raw",
            version="v1",
            source_version_policy="ohlcv_1d",
            dependencies=(Dependency("raw", "turnover"),),
            warmup=0,
            formula="turnover",
            aliases=("turnover",),
            materialization=Materialization.STORED,
        ),
        compute=_turnover,
    ),
)


def builtin_map() -> dict[str, BuiltinFactor]:
    return {item.spec.factor_id: item for item in PRICE_FACTORS}
