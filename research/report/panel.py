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
    pool_date: str,
    pool_size: int,
    start: str,
    end: str | None = None,
    *,
    min_amount: float | None = None,
    max_amount: float | None = None,
    warmup_days: int = 2000,
) -> Panel:
    requested_end = end
    end = _completed_cutoff(requested_end)
    # 因子窗口需要预热：504 日窗口若从评估起点才取数，前两年因子为 NaN，
    # 会把「42_252 vs 21_504」变成两个不同区间，制造 4 倍价差差异。
    eval_start = pd.Timestamp(start)
    data_start = (eval_start - pd.Timedelta(days=int(warmup_days))).date().isoformat()
    amount_filter = ""
    if min_amount is not None:
        amount_filter += f" and amount >= {min_amount}"
    if max_amount is not None:
        amount_filter += f" and amount <= {max_amount}"

    pool = _query(
        f"""select code, amount from ohlcv_1d
            where ts='{pool_date}' and type='stock' and market in ('SSE','SZSE'){amount_filter}
            order by amount desc limit {int(pool_size * 2.0)}"""
    )
    candidates = len(pool)
    codes = [str(c) for c in pool["code"] if str(c)[:2] in _ALLOWED_PREFIX][:pool_size]
    if not codes:
        raise RuntimeError(f"样本池为空：{pool_date} 无可用股票（检查 ohlcv_1d.type/market）")
    inlist = ",".join(f"'{c}'" for c in codes)

    frame = _query(
        f"""select ts, code, open, high, low, close, volume, amount, turnover, adj_factor
            from ohlcv_1d where type='stock' and code in ({inlist})
            and ts between '{data_start}' and '{end}'""",
        timeout=600,
    )
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # 后复权：以窗口末日的复权因子为 1（价格水平与当前一致，收益率不受影响）
    last_adj = frame.groupby("code")["adj_factor"].transform("last").replace(0, np.nan)
    frame["adj"] = (frame["adj_factor"] / last_adj).fillna(1.0)
    for column in ("open", "high", "low", "close"):
        frame[f"{column}_hfq"] = frame[column] * frame["adj"]

    close = _pivot(frame, "close_hfq").sort_index()
    open_ = _pivot(frame, "open_hfq").reindex(index=close.index, columns=close.columns)
    high = _pivot(frame, "high_hfq").reindex(index=close.index, columns=close.columns)
    low = _pivot(frame, "low_hfq").reindex(index=close.index, columns=close.columns)
    volume = _pivot(frame, "volume").reindex(index=close.index, columns=close.columns)
    amount = _pivot(frame, "amount").reindex(index=close.index, columns=close.columns)
    turnover = _pivot(frame, "turnover").reindex(index=close.index, columns=close.columns)
    close_raw = _pivot(frame, "close").reindex(index=close.index, columns=close.columns)
    open_raw = _pivot(frame, "open").reindex(index=close.index, columns=close.columns)

    # 末日覆盖检查：若最后一天的标的数不足前一日 80%，视为「未收盘/未入库」，
    # 整日回退。上一版报告把这种半截日当成完整交易日，导致 2.50%→2.29% 的口径漂移。
    coverage = close.notna().sum(axis=1)
    partial_last_day = None
    if len(coverage) >= 2 and coverage.iloc[-1] < 0.8 * max(int(coverage.iloc[-2]), 1):
        partial_last_day = {
            "date": str(close.index[-1].date()),
            "covered": int(coverage.iloc[-1]),
            "previous": int(coverage.iloc[-2]),
        }
        keep = close.index[:-1]
        close = close.loc[keep]
        open_ = open_.loc[keep]
        high = high.loc[keep]
        low = low.loc[keep]
        volume = volume.loc[keep]
        amount = amount.loc[keep]
        turnover = turnover.loc[keep]
        close_raw = close_raw.loc[keep]
        open_raw = open_raw.loc[keep]
        end = str(close.index.max().date())

    cap = _query(
        f"""select ts, code, float_market_cap from fin_valuation_daily
            where code in ({inlist}) and ts between '{start}' and '{end}'"""
    )
    market_cap = None
    if not cap.empty:
        cap["ts"] = pd.to_datetime(cap["ts"]).dt.tz_localize(None)
        cap["float_market_cap"] = pd.to_numeric(cap["float_market_cap"], errors="coerce")
        market_cap = _pivot(cap, "float_market_cap").reindex(index=close.index, columns=close.columns)

    info = _query(
        f"""select code, name, industry, list_date, delist_date, list_status, is_st
            from stock_basic_info where code in ({inlist}) latest on ts partition by code"""
    )
    industry = None
    is_st: dict[str, bool] = {}
    if not info.empty:
        info = info.drop_duplicates("code").set_index("code")
        mapping = info["industry"].fillna("未知").to_dict()
        industry = pd.DataFrame(
            [[mapping.get(c, "未知") for c in close.columns]] * len(close.index),
            index=close.index, columns=close.columns,
        )
        for code, row in info.iterrows():
            flag = row.get("is_st")
            is_st[str(code)] = bool(flag) if flag is not None and flag == flag else False

    regime = None
    benchmark = None
    # 000852 在 2019-2021 缺日，无法做全样本 regime；改用日线完整的创业板指 399006
    idx = _query(
        f"""select ts, close from ohlcv_1d where code='399006' and type='idx'
            and ts between '{start}' and '{end}' order by ts"""
    )
    if not idx.empty:
        idx["ts"] = pd.to_datetime(idx["ts"]).dt.tz_localize(None)
        idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
        idx = idx.dropna(subset=["close"]).drop_duplicates("ts").set_index("ts").sort_index()
        mom20 = idx["close"] / idx["close"].shift(20) - 1
        ma20 = idx["close"].rolling(20).mean()
        regime = pd.Series(
            np.where(
                (mom20 > 0) & (idx["close"] > ma20), "bull",
                np.where((mom20 < -0.03) & (idx["close"] < ma20), "bear", "neutral"),
            ),
            index=idx.index,
        ).reindex(close.index).ffill()
    # 沪深 300 敏感性（000300 已于 2026-10-30 回补 2019 起）
    regime_alt = None
    idx300 = _query(
        f"""select ts, close from ohlcv_1d where code='000300' and type='idx'
            and ts between '{start}' and '{end}' order by ts"""
    )
    if not idx300.empty:
        idx300["ts"] = pd.to_datetime(idx300["ts"]).dt.tz_localize(None)
        idx300["close"] = pd.to_numeric(idx300["close"], errors="coerce")
        idx300 = idx300.dropna(subset=["close"]).drop_duplicates("ts").set_index("ts").sort_index()
        mom = idx300["close"] / idx300["close"].shift(20) - 1
        ma = idx300["close"].rolling(20).mean()
        regime_alt = pd.Series(
            np.where(
                (mom > 0) & (idx300["close"] > ma), "bull",
                np.where((mom < -0.03) & (idx300["close"] < ma), "bear", "neutral"),
            ),
            index=idx300.index,
        ).reindex(close.index)

    # 基准用样本池等权（与组合同池，避免指数缺失/风格错配）
    benchmark = (1 + close.pct_change(fill_method=None).mean(axis=1)).cumprod()
    benchmark.name = "样本池等权"

    # 数据新鲜度：末日、是否回退、距今天数
    now = now_cn()
    last = close.index.max()
    freshness = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "北京时间 UTC+8",
        "requested_end": str(requested_end or ""),
        "cutoff": last.strftime("%Y-%m-%d"),
        "stale_natural_days": int((now.date() - last.date()).days),
        "intraday_trimmed": bool(requested_end and _completed_cutoff(requested_end) != str(requested_end)),
        "partial_last_day": partial_last_day,
        "last_day_coverage": int(close.notna().sum(axis=1).iloc[-1]),
        "today": now.strftime("%Y-%m-%d"),
    }

    selected = set(close.columns)
    if info is not None:
        selected_info = info.loc[[c for c in info.index if c in selected]]
    else:
        selected_info = pd.DataFrame()

    # 样本池构造与幸存者偏差
    pool_meta = {
        "kind": "price",
        "selection": "amount",
        "date": pool_date,
        "requested": int(pool_size),
        "candidates": int(candidates),
        "board_passed": int(len(codes)),
        "selected": int(len(close.columns)),
        "rule": f"{pool_date} 成交额降序取前 {pool_size} 只（2 倍缓冲后按板块过滤）",
        "boards": "沪深主板 / 创业板 / 科创板；剔除北交所",
        "start": str(start),
        "data_start": data_start,
        "end": str(last.date()),
        "funnel": [
            {"step": "基准日成交额降序候选（2 倍缓冲）", "n": int(candidates), "note": "含未上市/退市/北交所"},
            {"step": "板块过滤（60/00/30/68）", "n": int(len(codes)), "note": "剔除北交所与其它前缀"},
            {"step": "区间内有行情、进入报告", "n": int(len(close.columns)), "note": "最终样本"},
        ],
        "pit": "",
    }
    try:
        delisted = _query(
            f"""select count() c from stock_basic_info
                where list_status='D' and list_date <= '{pool_date}'
                latest on ts partition by code"""
        )
        n_delisted = int(delisted["c"].iloc[0]) if not delisted.empty else 0
        traded = _query(
            f"select distinct code from ohlcv_1d where ts='{pool_date}' and type='stock'"
        )
        traded_codes = {str(c) for c in traded["code"]} if not traded.empty else set()
        delisted_codes = _query(
            f"""select code from stock_basic_info
                where list_status='D' and list_date <= '{pool_date}'
                latest on ts partition by code"""
        )
        delisted_set = {str(c) for c in delisted_codes["code"]} if not delisted_codes.empty else set()
        pool_meta.update(
            {
                "delisted_universe": n_delisted,
                "delisted_with_price": len(delisted_set & traded_codes),
                "delisted_missing": len(delisted_set - traded_codes),
            }
        )
    except Exception:  # noqa: BLE001 - 元数据缺失不应中断报告
        pass
    if not selected_info.empty:
        list_dates = pd.to_datetime(selected_info.get("list_date"), errors="coerce", utc=True)
        list_dates = list_dates.dt.tz_convert(None)
        pool_meta["listed_after_start"] = int((list_dates > pd.Timestamp(start)).sum())
        pool_meta["delisted_in_pool"] = int((selected_info.get("list_status") == "D").sum())
    last_valid = close.apply(lambda s: s.last_valid_index())
    last_valid = pd.to_datetime(last_valid, errors="coerce")
    pool_meta["stale_symbols"] = int((last_valid < last - pd.Timedelta(days=10)).sum())
    pool_meta["survivorship_note"] = (
        "样本池按 2017-11-01 单日成交额固定，之后不再重构；库内退市股若没有历史行情，"
        "会被整体排除，导致收益与 IC 存在向上的幸存者偏差。该偏差无法用现有数据完全修正，"
        "只能通过固定入池日、披露缺失数量来限定解释边界。"
    )

    return Panel(
        close=close, open=open_, market_cap=market_cap, industry=industry, regime=regime,
        regime_alt=regime_alt,
        symbols=list(close.columns), high=high, low=low, volume=volume, amount=amount,
        turnover=turnover, close_raw=close_raw, open_raw=open_raw, benchmark=benchmark,
        stock_info=selected_info, is_st=is_st, pool=pool_meta, freshness=freshness,
        eval_start=eval_start,
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


def financial_factor_panel(factor_id: str, close: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """从 factor_value_daily（物化结果）取单因子面板。"""
    codes = ",".join(f"'{c}'" for c in close.columns)
    frame = _query(
        "select ts, code, value from factor_value_daily "
        f"where factor_id='{factor_id}' and ts between '{start}' and '{end}' "
        f"and code in ({codes})",
        timeout=600,
    )
    if frame.empty:
        return pd.DataFrame()
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return _pivot(frame, "value").reindex(index=close.index, columns=close.columns)


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
