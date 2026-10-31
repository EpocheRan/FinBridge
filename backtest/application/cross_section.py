"""横截面预选：向量化算排名 → 目标组合 + 候选集。

混合架构的第一层：
- 在本层完成“全 universe 排名 / 选股 / 权重”，只把**候选集**交给 PyBroker 执行；
- 候选集 = 所有调仓日入选标的的并集（通常远小于全 universe）；
- 同时产出每日 rank，供执行层做 buffer 退出（不必每 bar 重算排名）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtest.application.rebalance import RebalanceSchedule
from backtest.application.universe import UniverseSchedule, filter_by_membership
from backtest.domain.errors import ConfigError


@dataclass(frozen=True)
class CrossSectionPlan:
    candidates: tuple[str, ...]
    rebalance_dates: tuple[pd.Timestamp, ...]
    targets: Mapping[pd.Timestamp, Mapping[str, float]]
    ranks: pd.DataFrame
    scores: pd.DataFrame
    selections: pd.DataFrame
    universe_size: int
    top_n: int
    buffer: int

    def target_for(self, dt: Any) -> Mapping[str, float] | None:
        return self.targets.get(pd.Timestamp(dt).normalize())

    def rank_at(self, dt: Any, symbol: str) -> float | None:
        key = pd.Timestamp(dt).normalize()
        if key not in self.ranks.index or symbol not in self.ranks.columns:
            return None
        value = self.ranks.at[key, symbol]
        return None if pd.isna(value) else float(value)

    def score_at(self, dt: Any, symbol: str) -> float | None:
        key = pd.Timestamp(dt).normalize()
        if key not in self.scores.index or symbol not in self.scores.columns:
            return None
        value = self.scores.at[key, symbol]
        return None if pd.isna(value) else float(value)


def should_keep_buffer(rank: float | None, top_n: int, buffer: int) -> bool:
    """调仓日：排名在 top_n+buffer 内的既有持仓不因掉出 top_n 被卖。"""
    return rank is not None and float(rank) <= int(top_n) + int(buffer)


def _normalize_weights(
    weights: Mapping[str, float], gross_exposure: float
) -> dict[str, float]:
    values = {
        str(symbol): float(value)
        for symbol, value in (weights or {}).items()
        if np.isfinite(value) and float(value) > 0
    }
    if not values:
        return {}
    total = sum(values.values())
    cap = float(gross_exposure)
    if total > cap:
        values = {symbol: value * cap / total for symbol, value in values.items()}
    return values


def plan_cross_section(
    *,
    panel: Any,
    strategy: Any,
    symbols: Sequence[str],
    start: date,
    end: date,
    calendar: Sequence[pd.Timestamp],
    top_n: int,
    buffer: int,
    max_gross_exposure: float,
    rebalance: str,
    every_n_bars: int = 1,
    membership: UniverseSchedule | None = None,
) -> CrossSectionPlan:
    universe = tuple(dict.fromkeys(str(symbol) for symbol in symbols))
    if not universe:
        raise ConfigError("横截面 universe 为空")
    if int(top_n) <= 0:
        raise ConfigError("top_n 必须 > 0")
    if int(buffer) < 0:
        raise ConfigError("buffer 不能为负")

    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    dates = tuple(
        dt
        for dt in (pd.Timestamp(item).normalize() for item in calendar)
        if start_ts <= dt <= end_ts
    )
    if not dates:
        raise ConfigError(f"调仓区间内没有交易日: {start}..{end}")

    schedule = RebalanceSchedule.from_value(rebalance, every_n_bars=every_n_bars)
    rebalance_dates = tuple(schedule.dates(dates)) or (dates[0],)

    scores_by_date: dict[pd.Timestamp, pd.Series] = {}
    ranks_by_date: dict[pd.Timestamp, pd.Series] = {}
    for dt in dates:
        view = panel.view(dt)
        scores = strategy.rank(view, dt)
        if not isinstance(scores, pd.Series):
            raise ConfigError(
                f"strategy.rank 必须返回 Series，收到 {type(scores).__name__}"
            )
        scores = scores.reindex(universe).astype("float64")
        scores = scores[np.isfinite(scores)]
        if membership is not None and membership.has_membership:
            # PIT 成分过滤：非成分股当天不参与排名，也就不会被选中或按 buffer 保留。
            scores = filter_by_membership(scores, membership.members_at(dt))
        scores_by_date[dt] = scores
        ranks_by_date[dt] = scores.rank(ascending=False, method="first")

    targets: dict[pd.Timestamp, dict[str, float]] = {}
    selection_rows: list[dict[str, Any]] = []
    for dt in rebalance_dates:
        scores = scores_by_date.get(dt)
        if scores is None or scores.empty:
            continue
        ordered = scores.sort_values(ascending=False)
        selected = [str(symbol) for symbol in ordered.head(int(top_n)).index]
        view = panel.view(dt)
        raw_weights = strategy.weights(view, scores, selected)
        # 自定义 weights 只能影响本次入选标的，不能偷偷加入榜单外标的。
        raw_weights = {
            symbol: raw_weights[symbol]
            for symbol in selected
            if symbol in raw_weights
        }
        weights = _normalize_weights(raw_weights, max_gross_exposure)
        if not weights:
            continue
        targets[dt] = weights
        for symbol in selected:
            selection_rows.append(
                {
                    "date": dt,
                    "symbol": symbol,
                    "score": float(scores.get(symbol, float("nan"))),
                    "rank": float(ranks_by_date[dt].get(symbol, float("nan"))),
                    "weight": float(weights.get(symbol, 0.0)),
                    "selected": True,
                }
            )

    candidates = tuple(sorted({symbol for target in targets.values() for symbol in target}))
    if not candidates:
        raise ConfigError(
            "横截面计划为空：没有可执行的调仓目标。"
            "常见原因：① 因子整列无值（如 size.* 缺 float_mv 列、情绪因子缺两融/资金流数据）；"
            "② 方向/阈值把候选全过滤；③ 回测起点早于因子预热完成"
        )

    rank_frame = pd.DataFrame(
        {dt: ranks_by_date[dt].reindex(candidates) for dt in dates}
    ).T.sort_index()
    score_frame = pd.DataFrame(
        {dt: scores_by_date[dt].reindex(candidates) for dt in dates}
    ).T.sort_index()
    selections = pd.DataFrame(
        selection_rows,
        columns=["date", "symbol", "score", "rank", "weight", "selected"],
    )
    if not selections.empty:
        selections = selections.sort_values(["date", "rank"]).reset_index(drop=True)

    return CrossSectionPlan(
        candidates=candidates,
        rebalance_dates=tuple(sorted(targets)),
        targets=targets,
        ranks=rank_frame,
        scores=score_frame,
        selections=selections,
        universe_size=len(universe),
        top_n=int(top_n),
        buffer=int(buffer),
    )
