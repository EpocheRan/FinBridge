"""报告用数据面板：一次取数，供因子计算 / 中性化 / 净值 / 容量共用。

2026-10 起 ``ohlcv_1d.market`` 语义改为交易所（SSE/SZSE/BSE），股票用
``type='stock'`` 区分；本模块统一按新 schema 取数，并把价格换成后复权。

**预热**：行情从 ``start - warmup_days`` 取，因子算完再裁到 ``eval_start``；
否则 504 日窗口的因子会缺前两年，等于把不同变体放到不同评估区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from data_interface.common.timeutil import market_closed_today, now_cn

from .db import query as _query

#: 报告样本池只保留沪深主板/创业板/科创板；北交所 30% 涨跌停与流动性结构不同
_ALLOWED_PREFIX = ("60", "00", "30", "68")


# 取数已迁到 B 模块（research.eval.data），此处保留同名薄封装供旧调用方使用。
# 必须延迟导入：research.report.db → report.__init__ → runner → panel 存在环。
def financial_factor_panel(*args, **kwargs):
    from research.eval.data import stored_factor_panel

    return stored_factor_panel(*args, **kwargs)


def _pivot(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="ts", columns="code", values=column, aggfunc="last")


@dataclass
class Panel:
    close: pd.DataFrame
    open: pd.DataFrame
    market_cap: pd.DataFrame | None
    industry: pd.DataFrame | None
    regime: pd.Series | None
    symbols: list[str]
    regime_alt: pd.Series | None = None
    high: pd.DataFrame | None = None
    low: pd.DataFrame | None = None
    volume: pd.DataFrame | None = None
    amount: pd.DataFrame | None = None
    turnover: pd.DataFrame | None = None
    close_raw: pd.DataFrame | None = None
    open_raw: pd.DataFrame | None = None
    benchmark: pd.Series | None = None
    stock_info: pd.DataFrame | None = None
    is_st: dict[str, bool] = field(default_factory=dict)
    pool: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    #: 评估起点；close/open 会多取一段预热数据，因子面板在它之前的部分要裁掉
    eval_start: pd.Timestamp | None = None
    _frame: pd.DataFrame | None = field(default=None, repr=False)

    @property
    def data_cutoff(self) -> pd.Timestamp:
        return self.close.index.max()

    def frame(self) -> pd.DataFrame:
        """长表（ts × symbol），供按标的计算因子；只构建一次。"""
        if self._frame is not None:
            return self._frame
        close = self.close
        n_rows, n_cols = close.shape
        index = np.repeat(close.index.to_numpy(), n_cols)
        symbol = np.tile(close.columns.to_numpy(), n_rows)

        def flat(panel: pd.DataFrame | None) -> np.ndarray:
            if panel is None:
                return np.full(n_rows * n_cols, np.nan)
            return panel.reindex(index=close.index, columns=close.columns).to_numpy().ravel()

        data = {
            "ts": index,
            "symbol": symbol,
            "close": flat(close),
            "hfq_close": flat(close),
            "open": flat(self.open),
            "high": flat(self.high),
            "low": flat(self.low),
            "hfq_high": flat(self.high),
            "hfq_low": flat(self.low),
            "volume": flat(self.volume),
            "amount": flat(self.amount),
            "turnover": flat(self.turnover),
            "float_mv": flat(self.market_cap),
        }
        if self.benchmark is not None and not self.benchmark.empty:
            bench = self.benchmark.reindex(close.index).to_numpy(dtype="float64")
            data["benchmark_close"] = np.tile(bench, n_cols)
            data["benchmark_volume"] = np.ones(n_rows * n_cols)
        self._frame = pd.DataFrame(data)
        return self._frame


def _completed_cutoff(requested_end: str | None) -> str:
    """若请求的结束日是今天且尚未收盘，回退一天，避免用未收盘 K 线。"""
    today = now_cn().date()
    end = pd.Timestamp(requested_end).date() if requested_end else today
    if end > today:
        end = today
    if end == today and not market_closed_today():
        end = today - timedelta(days=1)
    return end.isoformat()


def load_panel(
    pool_date: str | None = None,
    pool_size: int | None = 200,
    start: str = "2019-01-01",
    end: str | None = None,
    *,
    min_amount: float | None = None,
    max_amount: float | None = None,
    warmup_days: int = 800,
    universe: str | None = None,
):
    """取数委托给唯一的 B 模块（``research.eval.data``）。

    本函数不再包含任何 SQL 或选池逻辑；``pool_date/pool_size`` 仅为兼容旧调用签名，
    实际池子由 ``universe``（默认 all_a 全市场）决定。
    """
    from .panel_adapter import load_panel as _load

    return _load(
        pool_date,
        pool_size,
        start,
        end,
        min_amount=min_amount,
        max_amount=max_amount,
        warmup_days=warmup_days,
        universe=universe,
    )


def price_factor_panel(factor_id: str, panel: Panel) -> pd.DataFrame:
    """按 symbol 分组计算价量因子，返回 date × symbol 面板。"""
    from factors.ops.price_factors import lookup

    fn, _window, _unit = lookup(factor_id)
    frame = panel.frame()
    wide = frame.groupby("symbol", group_keys=False).apply(
        lambda g: pd.Series(fn(g.reset_index(drop=True)).to_numpy(), index=g["ts"].values),
        include_groups=False,
    )
    packed = wide.T
    packed.index = pd.to_datetime(packed.index)
    out = packed.reindex(index=panel.close.index, columns=panel.close.columns)
    if panel.eval_start is not None:
        out = out.loc[out.index >= panel.eval_start]
    return out


def peer_panels(factor_id: str, panel: Panel) -> dict[str, pd.DataFrame]:
    """增量性对照组：同类因子 + 规模风格。"""
    from .definitions import peers_for

    peers: dict[str, pd.DataFrame] = {}
    for pid in peers_for(factor_id):
        try:
            if pid.split(".", 1)[0] in ("quality", "value", "growth", "valuation"):
                peers[pid] = financial_factor_panel(
                    pid, panel.close, str(panel.close.index.min().date()), str(panel.close.index.max().date())
                )
            else:
                peers[pid] = price_factor_panel(pid, panel)
        except Exception:  # noqa: BLE001
            continue
    if panel.market_cap is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            peers["style.log_float_mv"] = np.log(panel.market_cap.where(panel.market_cap > 0))
    return peers
