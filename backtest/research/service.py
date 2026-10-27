"""研究服务：从数据源加载价格/因子，产出 IC、分层、换手、衰减。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd

from factors import F, FactorDef, build_panel, required_warmup

from .metrics import (
    decay,
    forward_returns,
    ic,
    ic_summary,
    quantile_returns,
    turnover,
)

_FACTOR_PATTERN = re.compile(r"^(mom|ma|atr|vol)(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ResearchReport:
    factor: str
    horizons: tuple[int, ...]
    ic: dict[int, pd.Series]
    summary: pd.DataFrame
    quantiles: dict[int, pd.DataFrame]
    turnover: pd.Series
    decay: pd.DataFrame
    factor_frame: pd.DataFrame = field(repr=False)

    def frames(self) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {
            "ic_summary": self.summary,
            "turnover": self.turnover.to_frame("turnover"),
            "decay": self.decay,
        }
        for horizon, series in self.ic.items():
            result[f"ic_{horizon}"] = series.to_frame("ic")
        for horizon, frame in self.quantiles.items():
            result[f"quantiles_{horizon}"] = frame
        return result

    def write_csv(self, directory: str | Path) -> Path:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        for name, frame in self.frames().items():
            frame.to_csv(target / f"{name}.csv", index=True)
        return target


def parse_factor_name(text: str) -> FactorDef:
    match = _FACTOR_PATTERN.fullmatch(str(text).strip())
    if match is None:
        raise ValueError(
            f"无法识别因子 {text!r}；支持 mom/ma/atr/vol + 窗口，例如 mom20"
        )
    name, window = match.group(1).lower(), int(match.group(2))
    if window <= 0:
        raise ValueError("因子窗口必须 > 0")
    factory = {
        "mom": F.mom,
        "ma": F.ma,
        "atr": F.atr,
        "vol": F.vol,
    }[name]
    return factory(window)


def run_research(
    *,
    data_source,
    codes: Sequence[str],
    factor: str,
    start: date,
    end: date,
    freq: str = "d",
    horizons: Sequence[int] = (1, 5, 20),
    q: int = 5,
    top_n: int = 30,
) -> ResearchReport:
    definition = parse_factor_name(factor)
    warmup = required_warmup([definition])
    data = data_source.load(
        tuple(codes), start, end, warmup=warmup, freq=freq
    )
    panel = build_panel(data.frame, [definition], start, end)
    factor_frame = panel.values[definition.name]

    price_column = "hfq_close" if "hfq_close" in data.frame.columns else "close"
    close = data.frame.pivot_table(
        index="date",
        columns="symbol",
        values=price_column,
        aggfunc="last",
    ).sort_index()
    close.index = pd.to_datetime(close.index, errors="coerce").normalize()
    close = close.loc[
        (close.index >= pd.Timestamp(start).normalize())
        & (close.index <= pd.Timestamp(end).normalize())
    ]
    common_dates = factor_frame.index.intersection(close.index)
    common_symbols = factor_frame.columns.intersection(close.columns)
    factor_frame = factor_frame.loc[common_dates, common_symbols]
    close = close.loc[common_dates, common_symbols]

    horizon_list = tuple(sorted({int(item) for item in horizons if int(item) > 0}))
    forwards = forward_returns(close, horizon_list)
    ic_series = {
        horizon: ic(factor_frame, forward) for horizon, forward in forwards.items()
    }
    summary = pd.DataFrame.from_dict(
        {
            horizon: ic_summary(series)
            for horizon, series in ic_series.items()
        },
        orient="index",
    )
    summary.index.name = "horizon"
    quantiles = {
        horizon: quantile_returns(factor_frame, forward, q=q)
        for horizon, forward in forwards.items()
    }
    return ResearchReport(
        factor=str(factor),
        horizons=horizon_list,
        ic=ic_series,
        summary=summary,
        quantiles=quantiles,
        turnover=turnover(factor_frame, top_n=top_n),
        decay=decay(factor_frame, close, horizons=horizon_list),
        factor_frame=factor_frame,
    )
