"""行情数据增量更新编排。

与旧 ``DataLoader`` 的区别：

- 类名改为 ``MarketDataLoader``，只负责行情/基础信息/概念板块。
- 财务数据全部移交给 ``data_interface.financial``。
- 修复了旧版“基础信息 ts 判断失效”和“概念板块 OHLC 写错表”的问题。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.trading_calendar import TradingCalendar

from .fetcher import MarketDataFetcher, normalize_code

logger = logging.getLogger("DataInterface.MarketLoader")


class DataUpdateStrategy(ABC):
    @abstractmethod
    def get_expected_dates(self, loader: "MarketDataLoader", start_date: str, end_date: str, freq):
        ...

    @abstractmethod
    def update_database(self, fetcher: MarketDataFetcher, data: pd.DataFrame, freq=None):
        ...

    @abstractmethod
    def get_data(self, fetcher: MarketDataFetcher, stock_code: str, start_date: str, end_date: str, freq=None):
        ...


class OhlcUpdateStrategy(DataUpdateStrategy):
    def get_expected_dates(self, loader, start_date, end_date, freq):
        return loader._get_expected_dates(start_date, end_date, freq)

    def update_database(self, fetcher, data, freq=None):
        return fetcher.update_ohlc(data, freq)

    def get_data(self, fetcher, stock_code, start_date, end_date, freq=None):
        return fetcher.get_ohlc(stock_code, start_date, end_date, freq or "d")


class BasicInfoUpdateStrategy(DataUpdateStrategy):
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        return loader._get_expected_dates(start_date, end_date, "d")

    def update_database(self, fetcher, data, freq=None):
        if data.empty or "code" not in data.columns:
            return 0
        ticker = str(data["code"].iloc[0])
        ticker_type = fetcher.get_ticker_type(ticker)
        if ticker_type == "stock":
            return fetcher.update_stock_basic(data)
        if ticker_type in ("etf", "lof"):
            return fetcher.update_fund_basic(data)
        return 0

    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        if not stock_code:
            return pd.DataFrame()
        ticker_type = fetcher.get_ticker_type(stock_code)
        if ticker_type == "stock":
            return fetcher.get_stock_basic_info(code=stock_code)
        if ticker_type in ("etf", "lof"):
            return fetcher.get_fund_basic_info(code=stock_code)
        return pd.DataFrame()


class ConceptBoardsStrategy(DataUpdateStrategy):
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        return loader._get_expected_dates(start_date, end_date, "d")

    def update_database(self, fetcher, data, freq=None):
        return fetcher.update_concept_boards(data)

    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        return fetcher.get_concept_boards()


class ConceptBoardOhlcStrategy(DataUpdateStrategy):
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        return loader._get_expected_dates(start_date, end_date, "d")

    def update_database(self, fetcher, data, freq=None):
        return fetcher.update_concept_boards_ohlc(data)

    def get_data(self, fetcher, stock_code, start_date, end_date, freq=None):
        return fetcher.get_concept_boards_ohlc(stock_code, start_date, end_date)


class ConceptConstituentsStrategy(DataUpdateStrategy):
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        return loader._get_expected_dates(start_date, end_date, "d")

    def update_database(self, fetcher, data, freq=None):
        return fetcher.update_concept_constituents(data)

    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        return fetcher.get_concept_constituents(stock_code) if stock_code else pd.DataFrame()


class MarketDataLoader:
    def __init__(
        self,
        *,
        config: dict | None = None,
        fetcher: MarketDataFetcher | None = None,
        trading_calendar: TradingCalendar | None = None,
    ):
        self.config = config or get_config()
        self.fetcher = fetcher or MarketDataFetcher("questdb", config=self.config)
        self.trading_calendar = trading_calendar or TradingCalendar()
        self.ohlc_strategy = OhlcUpdateStrategy()
        self.basic_info_strategy = BasicInfoUpdateStrategy()
        self.concept_boards_strategy = ConceptBoardsStrategy()
        self.concept_board_ohlc_strategy = ConceptBoardOhlcStrategy()
        self.concept_constituents_strategy = ConceptConstituentsStrategy()

    # ------------------------------------------------------------------ 日期
    def _get_trading_days(self, start_date: str, end_date: str) -> list[str]:
        return self.trading_calendar.get_trading_days(start_date, end_date)

    def _calculate_expected_bars_per_day(self, freq: str) -> int | None:
        if freq == "d":
            return 1
        if freq.endswith("min"):
            return 240 // int(freq.replace("min", ""))
        return None

    def _get_expected_dates(self, start_date: str, end_date: str, freq: str):
        trading_days = self._get_trading_days(start_date, end_date)
        if freq in ("d",) or freq.endswith("min"):
            return trading_days, trading_days
        if freq == "w":
            days = []
            for year, week in {(d.year, d.week) for d in pd.to_datetime(trading_days)}:
                week_days = [
                    d
                    for d in pd.to_datetime(trading_days)
                    if d.year == year and d.week == week
                ]
                days.append(max(week_days).strftime("%Y-%m-%d"))
            return days, trading_days
        if freq == "m":
            days = []
            for year, month in {(d.year, d.month) for d in pd.to_datetime(trading_days)}:
                month_days = [
                    d
                    for d in pd.to_datetime(trading_days)
                    if d.year == year and d.month == month
                ]
                days.append(max(month_days).strftime("%Y-%m-%d"))
            return days, trading_days
        raise ValueError(f"不支持的频率类型: {freq}")

    @staticmethod
    def _merge_consecutive_trading_days(missing_dates, all_trading_days):
        if not missing_dates:
            return []
        all_days = [str(d) for d in all_trading_days]
        index = {date: idx for idx, date in enumerate(all_days)}
        missing = sorted(str(d) for d in missing_dates if str(d) in index)
        if not missing:
            return []

        ranges = []
        start = end = missing[0]
        previous_index = index[start]
        for date in missing[1:]:
            current_index = index[date]
            if current_index == previous_index + 1:
                end = date
            else:
                ranges.append((start, end))
                start = end = date
            previous_index = current_index
        ranges.append((start, end))
        return ranges

    def _check_missing_data(self, local_df, expected_dates, trading_days, freq):
        if freq is None:
            freq = "d"
        missing_dates = []
        if freq.endswith("min"):
            expected_bars = self._calculate_expected_bars_per_day(freq)
            if not local_df.empty:
                local_df = local_df.copy()
                local_df["date"] = local_df.index.strftime("%Y-%m-%d")
                counts = local_df.groupby("date").size()
                for date in expected_dates:
                    if date not in counts.index or counts[date] < expected_bars:
                        missing_dates.append(date)
            else:
                missing_dates = list(expected_dates)
        else:
            if not local_df.empty:
                local_dates = set(local_df.index.strftime("%Y-%m-%d"))
                missing_dates = [d for d in expected_dates if d not in local_dates]
            else:
                missing_dates = list(expected_dates)

        return self._merge_consecutive_trading_days(missing_dates, trading_days)

    # ------------------------------------------------------------------ 增量
    def _incremental_update(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        strategy: DataUpdateStrategy,
        freq: str | None = None,
    ) -> pd.DataFrame:
        questdb_fetcher = MarketDataFetcher("questdb", config=self.config)
        query_end = f"{end_date} 23:59"

        try:
            local_df = strategy.get_data(
                questdb_fetcher, stock_code, start_date, query_end, freq
            )
            logger.info("QuestDB 读取 %s: %s 行", stock_code, len(local_df))
        except Exception as exc:
            logger.error("QuestDB 查询失败 %s: %s", stock_code, exc)
            local_df = pd.DataFrame()

        try:
            expected_dates, trading_days = strategy.get_expected_dates(
                self, start_date, end_date, freq
            )
        except Exception as exc:
            logger.error("生成预期日期失败: %s", exc)
            expected_dates, trading_days = [], []

        missing_ranges = self._check_missing_data(
            local_df, expected_dates, trading_days, freq
        )

        api_df = pd.DataFrame()
        if missing_ranges:
            logger.info("发现 %s 个缺失区间", len(missing_ranges))
            source_priority = list(
                self.config["market"].get(
                    "incremental_source_priority", ["baostock", "akshare"]
                )
            )
            # BaoStock 不支持 1 分钟线和 ETF/LOF，直接走 AkShare
            if freq == "1min" or stock_code.startswith(("51", "15", "16", "18")):
                source_priority = ["akshare"]
            for range_start, range_end in missing_ranges:
                partial = pd.DataFrame()
                for source in source_priority:
                    try:
                        fetcher = MarketDataFetcher(source, config=self.config)
                        partial = strategy.get_data(
                            fetcher, stock_code, range_start, range_end, freq
                        )
                        if not partial.empty:
                            break
                    except Exception as exc:
                        logger.warning("数据源 %s 获取失败: %s", source, exc)
                if partial.empty:
                    logger.error("所有数据源均无法获取 %s ~ %s", range_start, range_end)
                else:
                    api_df = pd.concat([api_df, partial])

        if not api_df.empty:
            strategy.update_database(questdb_fetcher, api_df, freq)
            combined = (
                pd.concat([local_df, api_df])
                if not local_df.empty
                else api_df
            )
            combined = combined[~combined.index.duplicated(keep="first")].sort_index()
        else:
            combined = local_df

        logger.info(
            "数据完成 %s: 本地 %s + 增量 %s = %s 行",
            stock_code,
            len(local_df),
            len(api_df),
            len(combined),
        )
        return combined

    def _latest_trading_day_update(
        self, strategy: DataUpdateStrategy, stock_code: str | None = None
    ) -> pd.DataFrame:
        questdb_fetcher = MarketDataFetcher("questdb", config=self.config)
        try:
            db_data = strategy.get_data(questdb_fetcher, stock_code=stock_code)
        except Exception as exc:
            logger.error("数据库查询失败: %s", exc)
            db_data = pd.DataFrame()

        try:
            latest_day = self.trading_calendar.latest_trading_day()
        except Exception as exc:
            logger.error("获取最新交易日失败: %s", exc)
            latest_day = datetime.now().strftime("%Y-%m-%d")

        # 修复旧版：ts 已是 index，不再检查 columns
        if not db_data.empty and isinstance(db_data.index, pd.DatetimeIndex):
            last_update = db_data.index.max()
            if pd.notna(last_update) and last_update >= pd.to_datetime(latest_day):
                return db_data

        for source in self.config["market"].get(
            "incremental_source_priority", ["baostock", "akshare"]
        ):
            try:
                fetcher = MarketDataFetcher(source, config=self.config)
                api_data = strategy.get_data(fetcher, stock_code=stock_code)
                if not api_data.empty:
                    if "ts" not in api_data.columns:
                        api_data = api_data.copy()
                        api_data["ts"] = pd.Timestamp.now()
                    strategy.update_database(questdb_fetcher, api_data)
                    return api_data
            except Exception as exc:
                logger.warning("数据源 %s 获取最新基础信息失败: %s", source, exc)

        return db_data if not db_data.empty else pd.DataFrame()

    # ------------------------------------------------------------------ 对外
    def ohlc(
        self,
        stock_codes: str | list[str],
        start_date: str,
        end_date: str,
        freq: str = "d",
        now: bool = False,
    ) -> dict[str, pd.DataFrame]:
        if isinstance(stock_codes, str):
            stock_codes = [stock_codes]

        if now:
            current = datetime.now()
            is_trading_day = self.trading_calendar.is_trading_day(
                current.strftime("%Y-%m-%d")
            )
            is_trading_hour = "09:30" <= current.strftime("%H:%M") <= "23:59"
            if not (is_trading_day and is_trading_hour):
                now = False
                logger.info("当前非交易时间，自动切换 now=False")

        result: dict[str, pd.DataFrame] = {}
        for code in stock_codes:
            code = normalize_code(code)
            frame = self._incremental_update(
                code, start_date, end_date, self.ohlc_strategy, freq
            )
            if now:
                try:
                    fetcher = MarketDataFetcher("akshare", config=self.config)
                    realtime = fetcher.get_realtime_quotes(
                        ticker=code, source_type=fetcher.get_ticker_type(code)
                    )
                    if realtime is not None and not realtime.empty:
                        frame = pd.concat([realtime, frame])
                except Exception as exc:
                    logger.warning("拼接实时行情失败 %s: %s", code, exc)
            result[code] = frame
        return result

    def ticker_basic_info(self, stock_code: str) -> pd.DataFrame:
        if not stock_code:
            return pd.DataFrame()
        rows = self._latest_trading_day_update(
            self.basic_info_strategy, normalize_code(stock_code)
        )
        if rows.empty:
            return rows
        # 统一 ts 索引，便于 MCP 层 reset_index
        if "ts" in rows.columns:
            rows = rows.set_index("ts")
        return rows

    def concept_boards(self) -> pd.DataFrame:
        return self._latest_trading_day_update(self.concept_boards_strategy)

    def concept_boards_ohlc(
        self, board_names: str | list[str], start_date: str, end_date: str
    ) -> dict[str, pd.DataFrame]:
        if isinstance(board_names, str):
            board_names = [board_names]
        return {
            name: self._incremental_update(
                name, start_date, end_date, self.concept_board_ohlc_strategy
            )
            for name in board_names
        }

    def concept_constituents(self) -> pd.DataFrame:
        return self._latest_trading_day_update(
            self.concept_constituents_strategy
        )
