"""Infrastructure 适配器：把 data_interface 转成 application ports。"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Mapping, Sequence

import pandas as pd

from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import instrument_market
from data_interface.common.trading_calendar import TradingCalendar
from data_interface.market.adjust import apply_adjust
from data_interface.market.constants import ohlc_table_name
from data_interface.market.fetcher import MarketDataFetcher, normalize_code
from data_interface.market.loader import MarketDataLoader
from data_interface.market.sources.index_source import IndexMarketSource
from screener.config import ScreenerSettings
from screener.domain.clock import now_local
from screener.domain.models import CoverageReport, merge_date_ranges

logger = logging.getLogger("Screener.Acquisition")

_FREQ_ALIASES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}


def _ohlc_freq(freq: str) -> str:
    return _FREQ_ALIASES.get(str(freq), str(freq))


def _is_intraday(freq: str) -> bool:
    return _ohlc_freq(freq).endswith("min")


class TradingCalendarAdapter:
    def __init__(self, calendar: TradingCalendar | None = None):
        self.calendar = calendar or TradingCalendar()

    def trading_days(self, start: date, end: date) -> list[date]:
        values = self.calendar.get_trading_days(start.isoformat(), end.isoformat())
        return [date.fromisoformat(str(value)[:10]) for value in values]

    def shift(self, day: date, bars: int) -> date:
        if bars == 0:
            return day
        span = abs(bars) * 2 + 40
        if bars < 0:
            start = day - timedelta(days=span)
            end = day - timedelta(days=1)
            days = self.trading_days(start, end)
            if len(days) < abs(bars):
                start = day - timedelta(days=span * 2)
                days = self.trading_days(start, end)
            if len(days) < abs(bars):
                return start
            return days[-abs(bars)]
        start = day + timedelta(days=1)
        end = day + timedelta(days=span)
        days = self.trading_days(start, end)
        if len(days) < bars:
            end = day + timedelta(days=span * 2)
            days = self.trading_days(start, end)
        if len(days) < bars:
            return end
        return days[bars - 1]

    def latest_complete_trading_day(self, now: datetime | None = None) -> date:
        now = now or now_local()
        latest = self.calendar.latest_trading_day()
        latest_date = date.fromisoformat(str(latest)[:10])
        if latest_date == now.date() and now.time() < time(15, 10):
            previous = self.shift(latest_date, -1)
            return previous
        return latest_date


class MarketDataAdapter:
    def __init__(
        self,
        *,
        loader: MarketDataLoader,
        fetcher: MarketDataFetcher,
        db: QuestDBClient,
        calendar: TradingCalendar,
        settings: ScreenerSettings,
        index_source: IndexMarketSource | None = None,
    ):
        self.loader = loader
        self.fetcher = fetcher
        self.db = db
        self.calendar = calendar
        self.settings = settings
        self.index_source = index_source or IndexMarketSource(settings=settings)
        self._benchmark_unavailable_until = 0.0

    # ------------------------------------------------------------------ OHLC
    def ensure_ohlc(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        freq: str = "d",
        *,
        skip_dates: Mapping[str, set[date]] | None = None,
    ) -> dict[str, CoverageReport]:
        """补齐区间行情（DB 优先，第三方兜底）。

        Args:
            skip_dates: 每只股票「应该有但确实没有」的日期（停牌/退市/NoData
                负缓存）。这些日期不算缺口，避免每次构建都对停牌股重复回源。
        """
        codes = [normalize_code(str(code)) for code in codes]
        if not codes:
            return {}
        expected = self.calendar.get_trading_days(start.isoformat(), end.isoformat())
        expected_dates = {date.fromisoformat(str(item)[:10]) for item in expected}
        skipped = (
            {code: set(skip_dates.get(code, ())) for code in codes}
            if skip_dates
            else {}
        )

        def _missing(code: str, frame: pd.DataFrame) -> list[date]:
            if frame is None or frame.empty:
                available: set[date] = set()
            else:
                subset = frame[frame["code"] == code]
                available = set(
                    pd.to_datetime(subset["ts"], errors="coerce").dt.date.dropna()
                )
            return sorted(expected_dates - available - skipped.get(code, set()))

        frame = self.read_ohlc(codes, start, end, freq)
        missing_codes = [code for code in codes if _missing(code, frame)]

        if missing_codes:
            ohlc_freq = _ohlc_freq(freq)
            priority = (
                self.settings.backfill.intraday_source_priority
                if _is_intraday(freq)
                else self.settings.backfill.source_priority
            )
            # 长区间按配置分块，避免单次请求过大（Tushare 单接口有行数限制）
            for chunk_start, chunk_end in self._fetch_ranges(start, end, freq):
                try:
                    self.loader.ohlc(
                        missing_codes,
                        chunk_start.isoformat(),
                        chunk_end.isoformat(),
                        ohlc_freq,
                        now=False,
                        source_priority=list(priority),
                    )
                except Exception as exc:
                    logger.warning(
                        "ensure_ohlc 失败 %s %s~%s: %s",
                        freq,
                        chunk_start,
                        chunk_end,
                        exc,
                    )
            frame = self.read_ohlc(codes, start, end, freq)

        reports: dict[str, CoverageReport] = {}
        for code in codes:
            missing = _missing(code, frame)
            reports[code] = CoverageReport(
                dataset=f"ohlcv_{freq}",
                requested_codes=1,
                available_codes=0 if missing else 1,
                requested_start=start,
                requested_end=end,
                missing_ranges={code: merge_date_ranges(missing)} if missing else {},
                complete=not missing,
            )
        return reports

    def _fetch_ranges(
        self, start: date, end: date, freq: str
    ) -> list[tuple[date, date]]:
        """按 ``daily_chunk_years`` / ``minute_chunk_days`` 切分回源区间。"""
        if start > end:
            return []
        if _is_intraday(freq):
            step_days = max(1, int(self.settings.backfill.minute_chunk_days))
        else:
            step_days = 365 * max(1, int(self.settings.backfill.daily_chunk_years))
        step = timedelta(days=step_days)
        ranges: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + step - timedelta(days=1), end)
            ranges.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        return ranges

    def read_ohlc(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        freq: str = "d",
    ) -> pd.DataFrame:
        codes = [normalize_code(str(code)) for code in codes]
        if not codes:
            return pd.DataFrame()
        table = ohlc_table_name(_ohlc_freq(freq))
        fields = (
            "ts, code, open, high, low, close, volume, amount, turnover, adj_factor"
            if _ohlc_freq(freq) in ("d", "w", "m")
            else "ts, code, open, high, low, close, volume, amount, adj_factor"
        )
        frames: list[pd.DataFrame] = []
        for chunk_start in range(0, len(codes), 500):
            chunk = codes[chunk_start : chunk_start + 500]
            placeholders = ", ".join(["%s"] * len(chunk))
            sql = (
                f'SELECT {fields} FROM "{table}" '
                "WHERE ts BETWEEN %s AND %s "
                f"AND type <> 'idx' AND code IN ({placeholders}) "
                "ORDER BY ts, code"
            )
            params = [
                datetime.combine(start, dt_time.min),
                datetime.combine(end, dt_time.max).replace(microsecond=0),
                *chunk,
            ]
            try:
                frame = self.db.query_dataframe(sql, params)
            except Exception:
                # 存量库尚未迁移 adj_factor 列：退化为不复权读取。
                legacy_fields = fields.replace(", adj_factor", "")
                legacy_sql = sql.replace(fields, legacy_fields)
                frame = self.db.query_dataframe(legacy_sql, params)
                if frame is not None and not frame.empty:
                    frame["adj_factor"] = pd.NA
            if frame is not None and not frame.empty:
                if isinstance(frame.index, pd.DatetimeIndex):
                    frame = frame.reset_index()
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        frame = pd.concat(frames, ignore_index=True)
        return self._apply_qfq(frame)

    def _latest_adj_factors(self, codes: Sequence[str]) -> dict[str, float | None]:
        codes = [normalize_code(str(code)) for code in codes]
        if not codes:
            return {}
        result: dict[str, float | None] = {}
        for chunk_start in range(0, len(codes), 500):
            chunk = codes[chunk_start : chunk_start + 500]
            placeholders = ", ".join(["%s"] * len(chunk))
            sql = (
                "SELECT code, adj_factor FROM \"ohlcv_1d\" "
                f"WHERE code IN ({placeholders}) "
                "LATEST ON ts PARTITION BY code"
            )
            try:
                frame = self.db.query_dataframe(sql, chunk)
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            for _, row in frame.iterrows():
                value = row.get("adj_factor")
                result[str(row["code"])] = (
                    float(value) if pd.notna(value) else None
                )
        return result

    def _apply_qfq(self, frame: pd.DataFrame) -> pd.DataFrame:
        """把「不复权价 + adj_factor」转成前复权序列，保留 raw_close。"""
        if frame is None or frame.empty:
            return frame
        if "adj_factor" not in frame.columns:
            frame = frame.copy()
            frame["adj_factor"] = pd.NA
        anchors = self._latest_adj_factors(frame["code"].astype(str).unique())
        outputs: list[pd.DataFrame] = []
        for code, group in frame.groupby("code", sort=False):
            adjusted = apply_adjust(
                group,
                "qfq",
                anchor=anchors.get(str(code)),
            )
            adjusted["code"] = code
            outputs.append(adjusted)
        return pd.concat(outputs, ignore_index=True)

    # ------------------------------------------------------------------ 基准
    def ensure_benchmark(self, start: date, end: date) -> CoverageReport:
        code = self.settings.benchmark_code
        frame = self.read_benchmark(start, end)
        expected = self.calendar.get_trading_days(start.isoformat(), end.isoformat())
        expected_dates = {date.fromisoformat(str(item)[:10]) for item in expected}
        available = (
            set(pd.to_datetime(frame["ts"], errors="coerce").dt.date.dropna())
            if not frame.empty
            else set()
        )
        missing = sorted(expected_dates - available)

        # 指数源不可用时不要在每个 code/date chunk 里反复打网络。
        if missing and time.monotonic() >= self._benchmark_unavailable_until:
            ranges = merge_date_ranges(missing)
            wrote = False
            for date_range in ranges:
                try:
                    fetched = self.index_source.fetch_ohlc(
                        code, date_range.start, date_range.end
                    )
                    if fetched is None or fetched.empty:
                        self._benchmark_unavailable_until = time.monotonic() + 300
                        logger.warning("基准指数暂无数据，5 分钟内不再重试: %s", code)
                        break
                    if self._write_index_ohlc(fetched):
                        wrote = True
                except Exception as exc:
                    self._benchmark_unavailable_until = time.monotonic() + 300
                    logger.warning("基准指数补齐失败，5 分钟内不再重试 %s: %s", code, exc)
                    break
            if wrote:
                # QuestDB WAL 异步可见：写完立刻读会读不到，导致误报「不完整」
                deadline = time.monotonic() + 3.0
                while True:
                    frame = self.read_benchmark(start, end)
                    available = (
                        set(
                            pd.to_datetime(
                                frame["ts"], errors="coerce"
                            ).dt.date.dropna()
                        )
                        if not frame.empty
                        else set()
                    )
                    missing = sorted(expected_dates - available)
                    if not missing or time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)

        return CoverageReport(
            dataset="ohlcv_1d",
            requested_codes=1,
            available_codes=0 if missing else 1,
            requested_start=start,
            requested_end=end,
            missing_ranges={code: merge_date_ranges(missing)} if missing else {},
            complete=not missing,
        )

    def _write_index_ohlc(self, frame: pd.DataFrame) -> int:
        """指数无复权概念，直接写 ``market='IDX'``、``adj_factor=1``。"""
        if frame is None or frame.empty:
            return 0
        rows: list[tuple] = []
        for index, row in frame.iterrows():
            ts_value = row.get("ts") if "ts" in frame.columns else index
            if ts_value is None or pd.isna(ts_value):
                continue
            prices = [row.get(column) for column in ("open", "high", "low", "close")]
            if any(pd.isna(value) for value in prices):
                continue
            rows.append(
                (
                    pd.Timestamp(ts_value).strftime("%Y-%m-%d %H:%M:%S"),
                    float(prices[0]),
                    float(prices[1]),
                    float(prices[2]),
                    float(prices[3]),
                    int(float(row.get("volume") or 0)),
                    float(row.get("amount") or 0.0),
                    float(row.get("turnover") or 0.0),
                    1.0,
                    str(row.get("code", self.settings.benchmark_code)),
                    instrument_market(str(row.get("code", self.settings.benchmark_code))),
                    "idx",
                )
            )
        if not rows:
            return 0
        sql = (
            'INSERT INTO "ohlcv_1d" '
            "(ts, open, high, low, close, volume, amount, turnover, adj_factor, code, market, type) "
            "VALUES %s"
        )
        return self.db.execute_values(sql, rows, page_size=500)

    def read_benchmark(self, start: date, end: date) -> pd.DataFrame:
        sql = (
            'SELECT ts, code, open, high, low, close, volume, amount '
            'FROM "ohlcv_1d" '
            "WHERE type = 'idx' AND code = %s "
            "AND ts BETWEEN %s AND %s ORDER BY ts"
        )
        params = [
            self.settings.benchmark_code,
            datetime.combine(start, dt_time.min),
            datetime.combine(end, dt_time.max).replace(microsecond=0),
        ]
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return frame

    # ------------------------------------------------------------------ 参考
    def ensure_reference(self, codes: Sequence[str]) -> CoverageReport:
        codes = [normalize_code(str(code)) for code in codes]
        if not codes:
            return CoverageReport(dataset="stock_basic_info", complete=True)
        frame = self.read_reference(codes)
        available = set(frame["code"].astype(str)) if not frame.empty else set()
        missing = [code for code in codes if code not in available]
        if (
            missing
            and self.settings.backfill.fetch_missing_reference
            and self.settings.backfill.max_reference_fetch > 0
        ):
            for code in missing[: self.settings.backfill.max_reference_fetch]:
                try:
                    self.loader.ticker_basic_info(code)
                except Exception as exc:
                    logger.warning("基础信息补齐失败 %s: %s", code, exc)
            frame = self.read_reference(codes)
            available = set(frame["code"].astype(str)) if not frame.empty else set()
            missing = [code for code in codes if code not in available]
        return CoverageReport(
            dataset="stock_basic_info",
            requested_codes=len(codes),
            available_codes=len(codes) - len(missing),
            requested_start=None,
            requested_end=None,
            missing_ranges={code: [] for code in missing},
            complete=not missing,
        )

    def read_reference(self, codes: Sequence[str]) -> pd.DataFrame:
        codes = [normalize_code(str(code)) for code in codes]
        if not codes:
            return pd.DataFrame()
        placeholders = ", ".join(["%s"] * len(codes))
        sql = (
            "SELECT code, name, total_share, float_share, total_mv, float_mv, "
            "industry, board, is_st, list_status FROM \"stock_basic_info\" "
            f"WHERE code IN ({placeholders}) "
            "LATEST ON ts PARTITION BY code"
        )
        frame = self.db.query_dataframe(sql, codes)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index(drop=True)
        return frame

    def read_universe(
        self,
        market: str = "MLCN",
        *,
        exclude_st: bool = True,
        boards: Sequence[str] | None = None,
    ) -> list[str]:
        sql = (
            'SELECT code, board, is_st, list_status FROM "stock_basic_info" '
            "LATEST ON ts PARTITION BY code"
        )
        frame = self.db.query_dataframe(sql)
        if frame is None or frame.empty:
            fallback = self.db.query_dataframe(
                "SELECT DISTINCT code FROM \"ohlcv_1d\" WHERE type <> 'idx'"
            )
            if fallback is None or fallback.empty:
                return []
            return sorted(fallback["code"].astype(str).tolist())

        result = frame.copy()
        if "list_status" in result.columns:
            result = result[result["list_status"].fillna("L") == "L"]
        if exclude_st and "is_st" in result.columns:
            result = result[~result["is_st"].fillna(False).astype(bool)]
        if boards and "board" in result.columns:
            result = result[result["board"].isin(list(boards))]
        return sorted(result["code"].astype(str).str.split(".").str[0].unique())
