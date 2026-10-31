"""股票池解析：把 ``UniverseSpec`` 变成「取数代码 + 逐日成分」。

为什么需要这一层
----------------
回测原先只认 ``explicit``（写死代码列表），于是研究里「按 2017-11-01 成交额
取前 200 只」就被固化成回测池：入池日之后新上市的拿不到、退市的还留在池里，
而且指数成分变动完全不反映。这一层把「池子」和「成分」拆开：

- ``codes``：一次取数用的代码并集（只用于 SQL IN 列表）
- ``snapshots``：按时间排序的成分快照，调仓日用 ``members_at(dt)`` 取 PIT 成分

PIT 语义与 ``index_weight`` 一致：某日的成分 = 该日之前（含）最近一期快照。
快照之前的日期返回空集合，宁可不交易也不使用未来成分。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

import pandas as pd


@dataclass(frozen=True)
class MembershipSnapshot:
    """某一期的成分股快照。"""

    ts: pd.Timestamp
    symbols: frozenset[str]

    @classmethod
    def build(cls, ts: object, symbols: Iterable[str]) -> "MembershipSnapshot":
        return cls(
            ts=pd.Timestamp(ts).normalize(),
            symbols=frozenset(str(item) for item in symbols if str(item).strip()),
        )


@dataclass(frozen=True)
class UniverseSchedule:
    """解析后的股票池。

    ``snapshots`` 为空表示「不做成分过滤」（explicit / all_a：能用数据就用）。
    """

    codes: tuple[str, ...]
    snapshots: tuple[MembershipSnapshot, ...] = ()
    label: str = "explicit"

    def __post_init__(self) -> None:
        if not self.codes:
            raise ValueError("UniverseSchedule.codes 不能为空")
        ordered = tuple(sorted(self.snapshots, key=lambda item: item.ts))
        object.__setattr__(self, "snapshots", ordered)

    @property
    def has_membership(self) -> bool:
        return bool(self.snapshots)

    def members_at(self, dt: object) -> frozenset[str]:
        """PIT 成分：dt 之前（含）最近一期快照。无快照时返回全部 codes。"""
        if not self.snapshots:
            return frozenset(self.codes)
        key = pd.Timestamp(dt).normalize()
        chosen: MembershipSnapshot | None = None
        for snapshot in self.snapshots:  # 已按 ts 升序；快照数是月度量级
            if snapshot.ts <= key:
                chosen = snapshot
            else:
                break
        return frozenset() if chosen is None else chosen.symbols

    def snapshots_between(self, start: object, end: object) -> tuple[MembershipSnapshot, ...]:
        start_ts = pd.Timestamp(start).normalize()
        end_ts = pd.Timestamp(end).normalize()
        return tuple(item for item in self.snapshots if start_ts <= item.ts <= end_ts)

    def fingerprint(self) -> str:
        """池子指纹：进入 run_id，成分变动必须换 run_id。"""
        payload = [self.label, ",".join(self.codes)]
        payload.extend(f"{item.ts.date()}:{len(item.symbols)}" for item in self.snapshots)
        return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()[:16]


def explicit_schedule(codes: Sequence[str], label: str = "explicit") -> UniverseSchedule:
    return UniverseSchedule(codes=tuple(sorted({str(item) for item in codes})), label=label)


def schedule_from_snapshots(
    rows: Sequence[tuple[object, str]],
    *,
    label: str,
) -> UniverseSchedule:
    """把 (ts, symbol) 行聚成周期快照。

    rows 可以跨期重复；同一 ``ts`` 的 symbol 合并成一个快照。
    """
    grouped: dict[pd.Timestamp, set[str]] = {}
    for ts, symbol in rows:
        key = pd.Timestamp(ts).normalize()
        grouped.setdefault(key, set()).add(str(symbol))
    snapshots = tuple(
        MembershipSnapshot(ts=key, symbols=frozenset(values))
        for key, values in sorted(grouped.items())
    )
    codes = tuple(sorted({symbol for snapshot in snapshots for symbol in snapshot.symbols}))
    return UniverseSchedule(codes=codes, snapshots=snapshots, label=label)


def filter_by_membership(
    scores: pd.Series,
    members: frozenset[str],
) -> pd.Series:
    """只保留当日成分内的标的；members 为空时原样返回（非 PIT 池）。"""
    if not members:
        return scores
    keep = [symbol for symbol in scores.index if symbol in members]
    return scores.loc[keep]


__all__ = [
    "MembershipSnapshot",
    "UniverseSchedule",
    "explicit_schedule",
    "filter_by_membership",
    "schedule_from_snapshots",
]
