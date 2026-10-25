"""交易日历。

优先读 QuestDB 的 ``trading_days`` 表；缺失区间从 BaoStock 增量补齐。
表由 ``scripts/init_database.py`` 创建，程序运行期不再建表。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .baostock_client import BaoStockClient
from .questdb_client import QuestDBClient

logger = logging.getLogger("DataInterface.TradingCalendar")


class TradingCalendar:
    def __init__(
        self,
        db: QuestDBClient | None = None,
        baostock: BaoStockClient | None = None,
    ):
        self.db = db or QuestDBClient()
        self._baostock = baostock

    @property
    def baostock(self) -> BaoStockClient:
        if self._baostock is None:
            self._baostock = BaoStockClient()
        return self._baostock

    def _last_db_date(self) -> str | None:
        row = self.db.query("SELECT MAX(ts) FROM trading_days", fetch_all=False)
        if row and row[0]:
            return row[0].strftime("%Y-%m-%d")
        return None

    def _fetch_from_baostock(self, start_date: str, end_date: str) -> list[str]:
        rs = self.baostock.client.query_trade_dates(
            start_date=start_date, end_date=end_date
        )
        days: list[str] = []
        while rs.error_code == "0" and rs.next():
            date, is_trading = rs.get_row_data()
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
        last_date = self._last_db_date()
        start_fill = (
            "1990-01-01"
            if not last_date
            else (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
        )
        if last_date and start_fill > end_date:
            return

        trading_days = set(self._fetch_from_baostock(start_fill, end_date))
        if not trading_days:
            logger.warning("BaoStock 未返回交易日数据: %s ~ %s", start_fill, end_date)
            return

        all_dates = self._date_range(start_fill, end_date)
        rows = [
            (datetime.strptime(date, "%Y-%m-%d"), date in trading_days)
            for date in all_dates
        ]
        self.db.execute_many(
            "INSERT INTO trading_days (ts, is_trading) VALUES (%s, %s)", rows
        )
        logger.info("交易日历补齐 %s 条 (%s ~ %s)", len(rows), start_fill, end_date)

    def get_trading_days(self, start_date: str, end_date: str) -> list[str]:
        self._fill_missing_days(datetime.now().strftime("%Y-%m-%d"))
        rows = self.db.query(
            """
            SELECT DISTINCT ts
            FROM trading_days
            WHERE ts BETWEEN %s AND %s AND is_trading = true
            ORDER BY ts
            """,
            (start_date, end_date),
        )
        return [row[0].strftime("%Y-%m-%d") for row in rows]

    def is_trading_day(self, date: str) -> bool:
        return bool(self.get_trading_days(date, date))

    def latest_trading_day(self, lookback_days: int = 30) -> str:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%d"
        )
        days = self.get_trading_days(start_date, end_date)
        if not days:
            raise RuntimeError(f"无法获取最近 {lookback_days} 天的交易日")
        return days[-1]
