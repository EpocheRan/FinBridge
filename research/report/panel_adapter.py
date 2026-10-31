"""报告取数适配器：把 B（``research.eval.data``）的结果转成报告要的 ``Panel``。

报告层不再自己写 SQL：本模块只做「PanelData → Panel」的字段映射，
数据获取全部由唯一的 B 模块完成（分块查询，支持 all_a 全市场）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.eval.data import load_panel as _load_panel_data
from research.eval.spec import EvalSpec

from .panel import Panel

#: 报告默认池子：全市场（可用 ``--universe`` 覆盖）
DEFAULT_UNIVERSE = "all_a"


def _pivot(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """宽表透视：优先用 pivot（不聚合，C 实现），重复键再退回 pivot_table。

    B 模块的 ``_load_ohlcv`` 已按 (symbol, date) 去重，正常路径不会走到 fallback；
    36 次 pivot_table 每次约 3.9s，换 pivot 后降到亚秒级。
    """
    try:
        return frame.pivot(index="date", columns="symbol", values=column)
    except ValueError:
        return frame.pivot_table(index="date", columns="symbol", values=column, aggfunc="last")


def load_panel(
    pool_date: str | None = None,
    pool_size: int | None = None,
    start: str = "2020-01-01",
    end: str | None = None,
    *,
    min_amount: float | None = None,
    max_amount: float | None = None,
    warmup_days: int = 800,
    universe: str | None = None,
) -> Panel:
    """唯一取数入口（经 B 模块）；``pool_date/pool_size`` 仅为兼容旧签名。"""
    target = universe or DEFAULT_UNIVERSE
    if target == "pool" and pool_date and pool_size:
        target = f"pool:{pool_date}:{int(pool_size)}"
    spec = EvalSpec(
        universe=target,
        start=start,
        end=end,
        warmup_days=warmup_days,
        min_names=30,
    )
    data = _load_panel_data(spec)
    frame = data.frame

    close = _pivot(frame, "hfq_close").sort_index()
    close = close.reindex(index=data.dates)
    columns = close.columns
    open_ = _pivot(frame, "hfq_open").reindex(index=close.index, columns=columns)
    high = _pivot(frame, "hfq_high").reindex(index=close.index, columns=columns) if "hfq_high" in frame else close
    low = _pivot(frame, "hfq_low").reindex(index=close.index, columns=columns) if "hfq_low" in frame else close
    volume = _pivot(frame, "volume").reindex(index=close.index, columns=columns)
    amount = _pivot(frame, "amount").reindex(index=close.index, columns=columns)
    turnover = _pivot(frame, "turnover").reindex(index=close.index, columns=columns)
    close_raw = _pivot(frame, "close").reindex(index=close.index, columns=columns)
    open_raw = _pivot(frame, "open").reindex(index=close.index, columns=columns)
    market_cap = _pivot(frame, "float_mv").reindex(index=close.index, columns=columns) if "float_mv" in frame else None

    benchmark = (1 + close.pct_change(fill_method=None).mean(axis=1)).cumprod()
    benchmark.name = "样本池等权"

    pool_meta = {
        "kind": "universe",
        "selection": target,
        "date": pool_date or start,
        "requested": int(pool_size or len(columns)),
        "candidates": int(len(columns)),
        "board_passed": int(len(columns)),
        "selected": int(len(columns)),
        "rule": f"universe={target}（B 模块按需装配）",
        "boards": "全市场" if target == "all_a" else target,
        "start": str(start),
        "data_start": str(close.index.min().date()),
        "end": str(close.index.max().date()),
        "funnel": [
            {"step": "universe 解析", "n": int(len(data.codes)), "note": target},
            {"step": "区间内有行情", "n": int(len(columns)), "note": "进入报告"},
        ],
        "pit": "index: 为 PIT 成分快照；all_a 为区间内全市场（数据可得性即约束）",
        "survivorship_note": (
            "all_a 含区间内全部有行情的股票，退市股若库存无历史行情仍会缺失。"
        ),
    }
    freshness = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "北京时间 UTC+8",
        "requested_end": str(end or ""),
        "cutoff": str(close.index.max().date()),
        "stale_natural_days": 0,
        "intraday_trimmed": False,
        "partial_last_day": None,
        "last_day_coverage": int(close.notna().sum(axis=1).iloc[-1]),
        "today": str(pd.Timestamp.today().date()),
    }

    return Panel(
        close=close,
        open=open_,
        market_cap=market_cap,
        industry=None,
        regime=None,
        symbols=list(columns),
        high=high,
        low=low,
        volume=volume,
        amount=amount,
        turnover=turnover,
        close_raw=close_raw,
        open_raw=open_raw,
        benchmark=benchmark,
        stock_info=None,
        is_st={},
        pool=pool_meta,
        freshness=freshness,
        eval_start=pd.Timestamp(start),
    )


__all__ = ["DEFAULT_UNIVERSE", "load_panel"]
