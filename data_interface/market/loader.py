"""行情数据增量更新编排。

与旧版的差异（2026-10）：

- 库里存不复权价 + ``adj_factor``，读时按 ``adjust`` 计算复权，除权不再回刷历史。
- 复用同一个 fetcher / QuestDB 连接，不再为每只股票、每个缺失区间新建连接。
- 交易日历不可用时回退工作日（不再静默「无缺失」）。
- DB 读取失败直接抛出，不把故障伪装成空数据后全量回源。
- 周/月线用 ``Period`` 作为周期身份，且只生成**已完成**周期，避免半根 bar。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import SourceUnavailableError
from data_interface.common.security_code import is_fund_type, normalize_code
from data_interface.common.timeutil import (
    market_closed_today,
    now_cn,
    today_str,
    trading_session,
)
from data_interface.common.trading_calendar import TradingCalendar

from .adjust import DEFAULT_ADJUST, apply_adjust, normalize_adjust
from .adj_factors import AdjFactorProvider, attach_adj_factor
from .constants import normalize_frequency
from .fetcher import MarketDataFetcher

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
        if is_fund_type(ticker_type):
            return fetcher.update_fund_basic(data)
        return 0

    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        if not stock_code:
            return pd.DataFrame()
        ticker_type = fetcher.get_ticker_type(stock_code)
        if ticker_type == "stock":
            return fetcher.get_stock_basic_info(code=stock_code)
        if is_fund_type(ticker_type):
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


class MarketDataLoader:
    def __init__(
        self,
        *,
        config: dict | None = None,
        fetcher: MarketDataFetcher | None = None,
        trading_calendar: TradingCalendar | None = None,
        adj_factors: AdjFactorProvider | None = None,
    ):
        self.config = config or get_config()
        self.fetcher = fetcher or MarketDataFetcher("questdb", config=self.config)
        self.trading_calendar = trading_calendar or TradingCalendar(db=getattr(self.fetcher, "_db", None))
        self.adj_factors = adj_factors or AdjFactorProvider(self.config)
        self.ohlc_strategy = OhlcUpdateStrategy()
        self.basic_info_strategy = BasicInfoUpdateStrategy()
        self.concept_boards_strategy = ConceptBoardsStrategy()
        self.concept_board_ohlc_strategy = ConceptBoardOhlcStrategy()
        self._adjust_anchor_cache: dict[str, float | None] = {}

    # ------------------------------------------------------------------ 日期
    def _get_trading_days(self, start_date: str, end_date: str) -> list[str]:
        return self.trading_calendar.trading_days_or_weekdays(start_date, end_date)

    def _calculate_expected_bars_per_day(self, freq: str) -> int | None:
        if freq == "d":
            return 1
        if freq.endswith("min"):
            return 240 // int(freq.replace("min", ""))
        return None

    def _get_expected_dates(self, start_date: str, end_date: str, freq: str):
        """返回 (预期 bar 日期, 交易日列表)。"""
        trading_days = self._get_trading_days(start_date, end_date)
        if freq == "d" or freq.endswith("min"):
            return trading_days, trading_days
        if freq in ("w", "m"):
            return self._completed_period_ends(trading_days, freq), trading_days
        raise ValueError(f"不支持的频率类型: {freq}")

    @staticmethod
    def _completed_period_ends(trading_days: list[str], freq: str) -> list[str]:
        """已完成的周/月周期，取该周期内最后一个交易日作为 bar 时间。"""
        if not trading_days:
            return []
        rule = "W" if freq == "w" else "M"
        series = pd.Series(pd.to_datetime(trading_days))
        periods = series.dt.to_period(rule)
        current_period = pd.Timestamp(now_cn()).to_period(rule)

        ends: dict[pd.Period, str] = {}
        for day, period in zip(trading_days, periods):
            ends[period] = max(ends.get(period, ""), day)

        result: list[str] = []
        for period, last_day in sorted(ends.items()):
            if period >= current_period and not market_closed_today():
                # 当前周期还没走完（未收盘），先不生成 bar，避免写入半根
                continue
            result.append(last_day)
        return result

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

    @staticmethod
    def _merge_expected_ranges(missing_dates, expected_dates):
        """按周/月预期日期合并缺失区间，避免每一周都单独请求一次第三方。"""
        if not missing_dates:
            return []
        expected = sorted(str(d) for d in expected_dates)
        index = {date: idx for idx, date in enumerate(expected)}
        missing = sorted(
            (index[str(d)], str(d)) for d in missing_dates if str(d) in index
        )
        if not missing:
            return []

        ranges = []
        start = end = missing[0][1]
        previous_index = missing[0][0]
        for current_index, date in missing[1:]:
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

        if freq in ("w", "m"):
            return self._merge_expected_ranges(missing_dates, expected_dates)
        return self._merge_consecutive_trading_days(missing_dates, trading_days)

    # ------------------------------------------------------------------ 增量
    def _incremental_update(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        strategy: DataUpdateStrategy,
        freq: str | None = None,
        source_priority: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        questdb_fetcher = self.fetcher
        query_end = f"{end_date} 23:59"

        local_df = strategy.get_data(
            questdb_fetcher, stock_code, start_date, query_end, freq
        )
        logger.info("QuestDB 读取 %s: %s 行", stock_code, len(local_df))

        expected_dates, trading_days = strategy.get_expected_dates(
            self, start_date, end_date, freq
        )

        missing_ranges = self._check_missing_data(
            local_df, expected_dates, trading_days, freq
        )

        api_df = pd.DataFrame()
        if missing_ranges:
            logger.info("发现 %s 个缺失区间", len(missing_ranges))
            if source_priority is None:
                priority = list(
                    self.config["market"].get(
                        "incremental_source_priority", ["baostock", "akshare"]
                    )
                )
            else:
                priority = list(source_priority)
            ticker_type = questdb_fetcher.get_ticker_type(stock_code)
            if freq == "1min" or is_fund_type(ticker_type):
                priority = ["akshare"]
            source_priority = priority
            for range_start, range_end in missing_ranges:
                partial = pd.DataFrame()
                last_error: Exception | None = None
                for source in source_priority:
                    try:
                        fetcher = MarketDataFetcher(source, config=self.config)
                        supports = getattr(fetcher, "supports_frequency", None)
                        if callable(supports) and not supports(freq):
                            logger.debug("数据源 %s 不支持 %s，跳过", source, freq)
                            continue
                        partial = strategy.get_data(
                            fetcher, stock_code, range_start, range_end, freq
                        )
                        if not partial.empty:
                            break
                    except Exception as exc:
                        last_error = exc
                        logger.warning("数据源 %s 获取失败: %s", source, exc)
                if partial.empty:
                    logger.error(
                        "所有数据源均无法获取 %s ~ %s%s",
                        range_start,
                        range_end,
                        f": {last_error}" if last_error else "",
                    )
                else:
                    api_df = pd.concat([api_df, partial])

        if not api_df.empty:
            factors = self.adj_factors.daily_factors(stock_code, start_date, end_date)
            api_df = attach_adj_factor(api_df, factors)
            strategy.update_database(questdb_fetcher, api_df, freq)
            combined = pd.concat([local_df, api_df]) if not local_df.empty else api_df
            # 新抓取的数据优先（keep="last"），否则旧行会盖掉刚更新的因子/价格
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
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
        db_data = strategy.get_data(self.fetcher, stock_code=stock_code)

        latest_day = self.trading_calendar.latest_trading_day()

        if not db_data.empty and isinstance(db_data.index, pd.DatetimeIndex):
            last_update = db_data.index.max()
            if pd.notna(last_update) and last_update >= pd.to_datetime(latest_day):
                return db_data

        last_error: Exception | None = None
        for source in self.config["market"].get(
            "incremental_source_priority", ["baostock", "akshare"]
        ):
            try:
                fetcher = MarketDataFetcher(source, config=self.config)
                api_data = strategy.get_data(fetcher, stock_code=stock_code)
                if api_data is None or api_data.empty:
                    continue
                api_data = api_data.copy()
                if "ts" not in api_data.columns:
                    # 快照类数据按「最近交易日」落库，避免每次调用都新增一行
                    api_data["ts"] = pd.Timestamp(latest_day)
                if "source" not in api_data.columns:
                    api_data["source"] = source
                strategy.update_database(self.fetcher, api_data)
                return api_data
            except Exception as exc:
                last_error = exc
                logger.warning("数据源 %s 获取最新基础信息失败: %s", source, exc)

        if not db_data.empty:
            return db_data
        if last_error is not None:
            raise SourceUnavailableError(
                f"基础信息获取失败且库内无数据: {last_error}", source="market"
            ) from last_error
        return pd.DataFrame()

    # ------------------------------------------------------------------ 对外
    def ohlc(
        self,
        stock_codes: str | list[str],
        start_date: str,
        end_date: str,
        freq: str = "d",
        now: bool = False,
        adjust: str = DEFAULT_ADJUST,
        source_priority: Sequence[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        freq = normalize_frequency(freq)
        mode = normalize_adjust(adjust)
        if isinstance(stock_codes, str):
            stock_codes = [stock_codes]

        append_realtime = bool(now) and self._should_append_realtime(freq, end_date)

        result: dict[str, pd.DataFrame] = {}
        for code in stock_codes:
            code = normalize_code(code)
            if not code:
                continue
            frame = self._incremental_update(
                code,
                start_date,
                end_date,
                self.ohlc_strategy,
                freq,
                source_priority=source_priority,
            )
            if append_realtime:
                frame = self._append_realtime(frame, code)
            anchor = self._adjust_anchor(code, frame)
            result[code] = apply_adjust(frame, mode, anchor=anchor)
        return result

    def _should_append_realtime(self, freq: str, end_date: str) -> bool:
        # 先做零成本判断：周/月线不拼、历史区间不拼
        if freq in ("w", "m") or end_date < today_str():
            return False
        if not self.trading_calendar.is_trading_day(today_str()):
            return False
        current = now_cn()
        # 只允许连续竞价时段（09:30-11:30 / 13:00-15:00）；午休和收盘后
        # 不应该再拼一条“实时”bar。
        return trading_session(current.strftime("%H:%M"))

    def _append_realtime(self, frame: pd.DataFrame, code: str) -> pd.DataFrame:
        try:
            realtime = MarketDataFetcher("tencent", config=self.config).get_realtime_quotes(
                ticker=code
            )
        except Exception as exc:
            logger.warning("拼接实时行情失败 %s: %s", code, exc)
            return frame
        if realtime is None or realtime.empty:
            return frame

        realtime = realtime.copy()
        if not isinstance(realtime.index, pd.DatetimeIndex):
            realtime.index = pd.to_datetime(realtime.index)
        if not frame.empty and "adj_factor" in frame.columns:
            factors = pd.to_numeric(frame["adj_factor"], errors="coerce").dropna()
            realtime["adj_factor"] = factors.iloc[-1] if not factors.empty else pd.NA
        combined = pd.concat([frame, realtime])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.attrs.update(getattr(frame, "attrs", {}))
        return combined

    def _adjust_anchor(self, code: str, frame: pd.DataFrame) -> float | None:
        """qfq 锚点：优先用库里的最新因子，保证跨查询区间一致。"""
        if code in self._adjust_anchor_cache:
            cached = self._adjust_anchor_cache[code]
            if cached is not None:
                return cached
        anchor = None
        try:
            anchor = self.fetcher.latest_adj_factor(code)
        except Exception as exc:
            logger.debug("获取最新复权因子失败 %s: %s", code, exc)
        if anchor is None and not frame.empty and "adj_factor" in frame.columns:
            factors = pd.to_numeric(frame["adj_factor"], errors="coerce").dropna()
            anchor = float(factors.iloc[-1]) if not factors.empty else None
            frame.attrs["adjust_anchor_source"] = "frame"
        self._adjust_anchor_cache[code] = anchor
        return anchor

    def ticker_basic_info(self, stock_code: str) -> pd.DataFrame:
        if not stock_code:
            return pd.DataFrame()
        rows = self._latest_trading_day_update(
            self.basic_info_strategy, normalize_code(stock_code)
        )
        if rows.empty:
            return rows
        if "ts" in rows.columns and "ts" not in rows.index.names:
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
