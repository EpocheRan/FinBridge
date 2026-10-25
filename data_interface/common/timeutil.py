"""统一时间口径。

设计约定：

- A 股业务时间一律按 **Asia/Shanghai（UTC+8，无夏令时）** 计算，
  不依赖宿主机/Docker 容器的 TZ 设置（容器默认 UTC 会导致 ``today`` 差 8 小时）。
- 写库统一使用 **naive 北京时间**，与 QuestDB ``TIMESTAMP``、Tushare
  ``trade_date``（北京时间自然日）语义一致。
- 需要 ``tz-aware`` 时显式传 ``aware=True``，避免 naive/aware 混用导致
  ``TypeError: can't compare offset-naive and offset-aware datetimes``。

中国自 1991 年起不再使用夏令时，因此固定 +08:00 偏移等价于 IANA
``Asia/Shanghai``，同时避免 Windows / slim 镜像缺 tzdata 的问题。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_cn(*, aware: bool = False) -> datetime:
    """当前北京时间。默认返回 naive（用于写库/比较）。"""
    current = datetime.now(CN_TZ)
    return current if aware else current.replace(tzinfo=None)


def today_cn() -> date:
    return now_cn().date()


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def to_cn(value: datetime, *, aware: bool = False) -> datetime:
    """把任意 datetime 转成北京时间（naive 时视为已经是北京时间）。"""
    if value.tzinfo is None:
        return value if not aware else value.replace(tzinfo=CN_TZ)
    converted = value.astimezone(CN_TZ)
    return converted if aware else converted.replace(tzinfo=None)


def as_naive_cn(value: Any) -> datetime | None:
    """尽量把外部时间值转成 naive 北京时间；转换失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_cn(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        import pandas as pd

        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return to_cn(parsed.to_pydatetime())
    except Exception:
        return None


def trading_session(time_text: str) -> bool:
    """判断 ``HH:MM`` 是否落在 A 股连续竞价时段（09:30-11:30 / 13:00-15:00）。"""
    return ("09:30" <= time_text <= "11:30") or ("13:00" <= time_text <= "15:00")


def market_closed_today(reference: datetime | None = None) -> bool:
    """当天是否已过收盘（15:00 北京时间）。"""
    current = reference or now_cn()
    return current.strftime("%H:%M") >= "15:00"


def weekday_range(start_date: str, end_date: str) -> list[str]:
    """工作日序列（周一到周五），用于交易日历不可用时的兜底。"""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        return []
    days: list[str] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return days


def latest_weekday(reference: datetime | None = None) -> str:
    """最近一个工作日（含当天）。"""
    cursor = (reference or now_cn()).date()
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.strftime("%Y-%m-%d")
