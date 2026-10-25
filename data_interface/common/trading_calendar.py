"""交易日历。

优先读 QuestDB 的 ``trading_days`` 表；缺失区间从 BaoStock 增量补齐。
表由 ``scripts/init_database.py`` 创建，程序运行期不再建表。

两层语义（重要）：

- ``get_trading_days`` 返回**真实**交易日；区间内确实没有交易日（如春节周、
  未来日期）时返回空列表，这属于正常结果。
- ``trading_days_or_weekdays`` 在日历**不可用/未覆盖**时回退到工作日序列，
  并记录 warning；调用方（增量行情、估值同步）必须用它，避免"日历空 →
  静默一行不抓"。

BaoStock 失败有 5 分钟退避，避免每个请求都打一次外网。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timedelta

from .baostock_client import BaoStockClient, get_shared_baostock_client
from .exceptions import SourceUnavailableError
from .questdb_client import QuestDBClient
from .timeutil import (
    latest_weekday,
    now_cn,
    today_str,
    weekday_range,
)

logger = logging.getLogger("DataInterface.TradingCalendar")

_CALENDAR_START = "1990-01-01"
_LAST_DATE_TTL_SECONDS = 300.0
_FILL_FAILURE_BACKOFF_SECONDS = 300.0


class TradingCalendar:
    def __init__(
        self,
        db: QuestDBClient | None = None,
        baostock: BaoStockClient | None = None,
    ):
        self.db = db or QuestDBClient()
        self._baostock = baostock
        self._last_date: str | None = None
        self._last_date_at: float = 0.0
        self._fill_failed_at: float = 0.0
        self._fill_error: str | None = None
        self._cache: dict[tuple[str, str], list[str]] = {}

    @property
    def baostock(self) -> BaoStockClient:
        if self._baostock is None:
            self._baostock = get_shared_baostock_client()
        return self._baostock

    # ------------------------------------------------------------------ 元数据
    def _refresh_last_date(self) -> str | None:
        now = time.monotonic()
        if self._last_date is not None and now - self._last_date_at < _LAST_DATE_TTL_SECONDS:
            return self._last_date
        try:
            row = self.db.query("SELECT MAX(ts) FROM trading_days", fetch_all=False)
        except Exception as exc:  # 表不存在/连接失败：视为未覆盖
            logger.warning("交易日历 MAX(ts) 查询失败: %s", exc)
            self._last_date = None
            self._last_date_at = now
            return None
        self._last_date = row[0].strftime("%Y-%m-%d") if row and row[0] else None
        self._last_date_at = now
        return self._last_date

    def last_covered_date(self) -> str | None:
        return self._refresh_last_date()

    def has_calendar_coverage(self, end_date: str) -> bool:
        """日历是否已覆盖到 ``end_date``（用于区分「没数据」和「没日历」）。"""
        last = self._refresh_last_date()
        return bool(last and last >= end_date)

    @property
    def last_fill_error(self) -> str | None:
        return self._fill_error

    # ------------------------------------------------------------------ 补齐
    def _fetch_from_baostock(self, start_date: str, end_date: str) -> list[str]:
        result = self.baostock.client.query_trade_dates(
            start_date=start_date, end_date=end_date
        )
        if result.error_code != "0":
            raise SourceUnavailableError(
                f"BaoStock 交易日查询失败: {result.error_msg}", source="baostock"
            )
        days: list[str] = []
        while result.next():
            date, is_trading = result.get_row_data()
            if is_trading == "1":
                days.append(date)
        return days

    def _date_range(self, start_date: str, end_date: str) -> list[str]:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        return [
            (start + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((end - start).days + 1)
        ]

    def _fill_missing_days(self, end_date: str) -> None:
        """把 ``trading_days`` 补齐到 ``end_date``；失败时退避并记录错误。"""
        if time.monotonic() - self._fill_failed_at < _FILL_FAILURE_BACKOFF_SECONDS:
            return

        last_date = self._refresh_last_date()
        start_fill = (
            _CALENDAR_START
            if not last_date
            else (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
        )
        if last_date and start_fill > end_date:
            return

        try:
            trading_days = set(self._fetch_from_baostock(start_fill, end_date))
        except Exception as exc:
            self._fill_failed_at = time.monotonic()
            self._fill_error = str(exc)
            logger.warning("交易日历补齐失败（%.0fs 内不再重试）: %s", _FILL_FAILURE_BACKOFF_SECONDS, exc)
            return

        if not trading_days:
            self._fill_failed_at = time.monotonic()
            self._fill_error = f"BaoStock 在 {start_fill} ~ {end_date} 未返回交易日"
            logger.warning(self._fill_error)
            return

        rows = [
            (datetime.strptime(date, "%Y-%m-%d"), date in trading_days)
            for date in self._date_range(start_fill, end_date)
        ]
        try:
            self.db.execute_values(
                "INSERT INTO trading_days (ts, is_trading) VALUES %s", rows
            )
        except Exception as exc:
            self._fill_failed_at = time.monotonic()
            self._fill_error = str(exc)
            logger.warning("交易日历写入失败: %s", exc)
            return

        logger.info("交易日历补齐 %s 条 (%s ~ %s)", len(rows), start_fill, end_date)
        self._last_date = None
        self._last_date_at = 0.0
        self._cache.clear()
        self._fill_error = None

    # ------------------------------------------------------------------ 查询
    def get_trading_days(
        self, start_date: str, end_date: str, *, auto_fill: bool = True
    ) -> list[str]:
        """返回区间内真实交易日（可能为空：非交易日区间/未来日期）。"""
        key = (start_date, end_date)
        if key in self._cache:
            return list(self._cache[key])

        if auto_fill:
            self._fill_missing_days(today_str())

        rows = self.db.query(
            """
            SELECT DISTINCT ts
            FROM trading_days
            WHERE ts BETWEEN %s AND %s AND is_trading = true
            ORDER BY ts
            """,
            (start_date, end_date),
        )
        days = [row[0].strftime("%Y-%m-%d") for row in rows]
        self._cache[key] = days
        return list(days)

    def trading_days_or_weekdays(self, start_date: str, end_date: str) -> list[str]:
        """交易日优先；日历不可用/未覆盖时回退工作日，并记录 warning。

        该方法是**增量抓取/估值同步**唯一应该使用的入口。
        """
        try:
            days = self.get_trading_days(start_date, end_date)
        except Exception as exc:
            logger.warning("交易日历不可用，回退工作日: %s", exc)
            return weekday_range(start_date, end_date)

        if days:
            return days
        if self.has_calendar_coverage(end_date):
            return []  # 日历覆盖了该区间，确实没有交易日
        logger.warning(
            "交易日历未覆盖 %s ~ %s（last=%s），回退工作日", start_date, end_date, self.last_covered_date()
        )
        return weekday_range(start_date, end_date)

    def is_trading_day(self, date: str) -> bool:
        try:
            days = self.get_trading_days(date, date)
        except Exception as exc:
            logger.warning("交易日判断失败，回退工作日: %s", exc)
            return datetime.strptime(date, "%Y-%m-%d").weekday() < 5
        if days:
            return True
        if self.has_calendar_coverage(date):
            return False
        return datetime.strptime(date, "%Y-%m-%d").weekday() < 5

    def latest_trading_day(self, lookback_days: int = 30) -> str:
        end_date = today_str()
        start_date = (now_cn() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        days = self.trading_days_or_weekdays(start_date, end_date)
        if days:
            return days[-1]
        return latest_weekday()


def iter_trading_days(days: Iterable[datetime]) -> list[str]:
    """辅助：datetime 序列转 ``YYYY-MM-DD``。"""
    return [item.strftime("%Y-%m-%d") for item in days]
