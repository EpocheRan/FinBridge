"""统一取数：股票池解析、PIT 成分、面板帧（含可选辅助数据列）。

取数只走 ``research.report.db.query``（唯一 REST 入口），不 import backtest，
避免报告层与回测层互相绑死。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from research.report.db import query as _query

from .spec import EvalSpec

_CHUNK = 300

_EXTRA_SQL: dict[str, dict[str, str]] = {
    "margin_detail": {
        "rzye": "margin_rzye",
        "rzmre": "margin_rzmre",
        "rqye": "margin_rqye",
        "rqyl": "margin_rqyl",
    },
    "moneyflow_daily": {
        "buy_sm_amount": "mf_buy_sm",
        "sell_sm_amount": "mf_sell_sm",
        "buy_lg_amount": "mf_buy_lg",
        "sell_lg_amount": "mf_sell_lg",
        "buy_elg_amount": "mf_buy_elg",
        "sell_elg_amount": "mf_sell_elg",
        "net_mf_amount": "mf_net_amount",
    },
    "fin_valuation_daily": {
        "market_cap": "total_mv",
        "float_market_cap": "float_mv",
    },
}


@dataclass(frozen=True)
class Membership:
    """PIT 成分：某日成分 = 该日之前（含）最近一期快照；首期前为空。"""

    snapshots: tuple[tuple[pd.Timestamp, frozenset[str]], ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.snapshots)

    def at(self, dt: object) -> frozenset[str] | None:
        if not self.snapshots:
            return None
        key = pd.Timestamp(dt).normalize()
        chosen: frozenset[str] | None = None
        for ts, symbols in self.snapshots:
            if ts <= key:
                chosen = symbols
            else:
                break
        return chosen if chosen is not None else frozenset()


@dataclass
class PanelData:
    frame: pd.DataFrame
    membership: Membership
    dates: pd.DatetimeIndex
    codes: tuple[str, ...]
    label: str


def _literal(codes: list[str]) -> str:
    return ",".join(f"'{code}'" for code in codes)


def _stamp(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).normalize()


def _index_membership(index_code: str, start: str, end: str) -> tuple[list[str], Membership]:
    names = [index_code]
    if "." not in index_code:
        names += [f"{index_code}.SH", f"{index_code}.SZ"]
    start_ts = _stamp(start) - pd.Timedelta(days=400)
    frame = _query(
        "select ts, code from index_weight "
        f"where index_code in ({_literal(names)}) "
        f"and ts between '{start_ts.date()}' and '{end}' order by ts"
    )
    if frame.empty:
        raise RuntimeError(f"index_weight 没有 {index_code} 的成分（{start}..{end}）")
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None).dt.normalize()
    grouped: dict[pd.Timestamp, set[str]] = {}
    for ts, code in zip(frame["ts"], frame["code"].astype(str)):
        grouped.setdefault(ts, set()).add(code)
    snapshots = tuple((ts, frozenset(grouped[ts])) for ts in sorted(grouped))
    codes = sorted({code for _, symbols in snapshots for code in symbols})
    return codes, Membership(snapshots=snapshots)


def _pool_codes(pool_date: str, size: int) -> list[str]:
    frame = _query(
        "select code from ohlcv_1d "
        f"where ts='{pool_date}' and type='stock' and market in ('SSE','SZSE') "
        f"order by amount desc limit {int(size * 2)}"
    )
    codes = [str(code) for code in frame["code"] if str(code)[:2] in ("60", "00", "30", "68")]
    return codes[:size]



def _all_a_codes(start: str, end: str) -> list[str]:
    """全市场：区间内有行情的全部股票（type='stock'）。"""
    frame = _query(
        "select distinct code from ohlcv_1d "
        f"where type='stock' and ts between '{start}' and '{end}'"
    )
    return sorted(str(code) for code in frame["code"])


def _resolve_universe(spec: EvalSpec) -> tuple[list[str], Membership, str]:
    text = str(spec.universe).strip()
    if text in ("all_a", "all"):
        return _all_a_codes(spec.start, spec.end or _today()), Membership(), "all_a (whole market)"
    if text.startswith("index:"):
        code = text.split(":", 1)[1]
        codes, membership = _index_membership(code, spec.start, spec.end or _today())
        return codes, membership, f"index:{code}（PIT 成分）"
    if text.startswith("pool:"):
        _, date_text, size_text = text.split(":")
        codes = _pool_codes(date_text, int(size_text))
        return codes, Membership(), f"pool:{date_text}:{size_text}（固定池，含幸存者偏差）"
    if text.startswith("explicit:"):
        codes = [code.strip() for code in text.split(":", 1)[1].split(",") if code.strip()]
        return codes, Membership(), "explicit"
    raise ValueError(f"未知 universe 写法: {spec.universe!r}")


def _today() -> str:
    return pd.Timestamp.today().normalize().date().isoformat()


def _load_ohlcv(codes: list[str], start: str, end: str, warmup_days: int) -> pd.DataFrame:
    start_ts = (_stamp(start) - pd.Timedelta(days=int(warmup_days))).date().isoformat()
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(codes), _CHUNK):
        chunk = codes[offset : offset + _CHUNK]
        frame = _query(
            "select ts, code, open, high, low, close, volume, amount, turnover, adj_factor "
            f"from ohlcv_1d where type='stock' and code in ({_literal(chunk)}) "
            f"and ts between '{start_ts}' and '{end}'",
            timeout=600,
        )
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError(f"ohlcv_1d 无数据: {start}..{end}")
    data = pd.concat(frames, ignore_index=True)
    data["ts"] = pd.to_datetime(data["ts"]).dt.tz_localize(None).dt.normalize()
    data = data.rename(columns={"ts": "date", "code": "symbol"})
    for column in ("open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["date", "symbol"])
    data = data.drop_duplicates(["symbol", "date"], keep="last")
    return data.sort_values(["symbol", "date"]).reset_index(drop=True)


def _attach_extras(frame: pd.DataFrame, codes: list[str], start: str, end: str,
                   extras: tuple[str, ...]) -> pd.DataFrame:
    start_ts = (_stamp(start) - pd.Timedelta(days=800)).date().isoformat()
    for dataset in extras:
        mapping = _EXTRA_SQL.get(dataset)
        if not mapping:
            continue
        fields = ", ".join(mapping)
        parts: list[pd.DataFrame] = []
        for offset in range(0, len(codes), _CHUNK):
            chunk = codes[offset : offset + _CHUNK]
            part = _query(
                f"select ts, code, {fields} from {dataset} "
                f"where code in ({_literal(chunk)}) "
                f"and ts between '{start_ts}' and '{end}'",
                timeout=600,
            )
            if not part.empty:
                parts.append(part)
        if not parts:
            continue
        aux = pd.concat(parts, ignore_index=True)
        aux["ts"] = pd.to_datetime(aux["ts"]).dt.tz_localize(None).dt.normalize()
        aux = aux.rename(columns={"ts": "date", "code": "symbol", **mapping})
        for column in mapping.values():
            aux[column] = pd.to_numeric(aux[column], errors="coerce")
        aux = aux.drop_duplicates(["symbol", "date"], keep="last")
        frame = frame.merge(aux, on=["symbol", "date"], how="left")
    return frame


def load_panel(spec: EvalSpec) -> PanelData:
    """按统一口径取一次数，供所有因子共用（保证同池、同期、同列）。"""
    end = spec.end or _today()
    codes, membership, label = _resolve_universe(spec)
    frame = _load_ohlcv(codes, spec.start, end, spec.warmup_days)
    if spec.extras:
        frame = _attach_extras(frame, codes, spec.start, end, spec.extras)

    # 后复权：以区间末的复权因子为 1（价格水平对齐当前，收益率不受影响）
    factor = frame["adj_factor"].fillna(1.0)
    anchor = frame.groupby("symbol")["adj_factor"].transform("last").fillna(1.0)
    adjust = (factor / anchor.replace(0, np.nan)).fillna(1.0)
    for column in ("open", "high", "low", "close"):
        frame[f"hfq_{column}"] = frame[column] * adjust
    frame["hfq_close"] = frame["hfq_close"].fillna(frame["close"] * adjust)

    dates = pd.DatetimeIndex(sorted(frame["date"].unique()))
    dates = dates[dates >= _stamp(spec.start)]
    return PanelData(
        frame=frame,
        membership=membership,
        dates=dates,
        codes=tuple(sorted(frame["symbol"].unique())),
        label=label,
    )


__all__ = ["Membership", "PanelData", "load_panel"]


def stored_factor_panel(factor_id: str, close: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
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
