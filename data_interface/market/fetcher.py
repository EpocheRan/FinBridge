"""行情数据获取器。

职责：多源调度 + QuestDB 读写；第三方接口细节放在 ``sources`` 下。

存储契约：OHLCV 表只写**不复权价 + adj_factor**；复权在读取端计算。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import DataNotFoundError, SourceUnavailableError
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import (
    normalize_code,
    ticker_type as _ticker_type,
)
from data_interface.common.timeutil import now_cn

from .adjust import apply_adjust, normalize_adjust
from .constants import (
    MARKET_CN,
    OHLC_COLUMNS_DAILY,
    OHLC_COLUMNS_MINUTE,
    ohlc_table_name,
    normalize_frequency,
)
from .sources import (
    AkshareMarketSource,
    BaoStockMarketSource,
    SinaMinuteSource,
    TencentQuoteSource,
    TushareMarketSource,
)

logger = logging.getLogger("DataInterface.MarketFetcher")

__all__ = ["MarketDataFetcher", "normalize_code"]

_ADJ_FACTOR_TTL_SECONDS = 300.0


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _ts_to_text(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


class MarketDataFetcher:
    def __init__(
        self,
        source: str = "questdb",
        *,
        config: dict | None = None,
        db: QuestDBClient | None = None,
        akshare: AkshareMarketSource | None = None,
        baostock: BaoStockMarketSource | None = None,
        tencent: TencentQuoteSource | None = None,
        tushare: TushareMarketSource | None = None,
        sina: SinaMinuteSource | None = None,
    ):
        self.config = config or get_config()
        self.source = source
        # 只有真正用到 DB 时才建连接（P2：每个 fetcher 都建连接会耗尽 QuestDB 连接数）
        self._db = db
        self._akshare = akshare
        self._baostock = baostock
        self._tencent = tencent
        self._tushare = tushare
        self._sina = sina
        self._adj_cache: dict[str, tuple[float | None, float]] = {}

    # ------------------------------------------------------------------ 依赖
    @property
    def db(self) -> QuestDBClient:
        if self._db is None:
            self._db = QuestDBClient(self.config)
        return self._db

    @property
    def akshare(self) -> AkshareMarketSource:
        if self._akshare is None:
            self._akshare = AkshareMarketSource(self.config)
        return self._akshare

    @property
    def baostock(self) -> BaoStockMarketSource:
        if self._baostock is None:
            self._baostock = BaoStockMarketSource()
        return self._baostock

    @property
    def tencent(self) -> TencentQuoteSource:
        if self._tencent is None:
            self._tencent = TencentQuoteSource(
                int(self.config["market"].get("request_timeout_seconds", 8))
            )
        return self._tencent

    @property
    def tushare(self) -> TushareMarketSource:
        if self._tushare is None:
            self._tushare = TushareMarketSource(self.config)
        return self._tushare

    @property
    def sina(self) -> SinaMinuteSource:
        if self._sina is None:
            self._sina = SinaMinuteSource(
                float(self.config.get("market", {}).get("request_timeout_seconds", 8))
            )
        return self._sina

    def supports_frequency(self, freq: str) -> bool:
        """该数据源是否支持指定频率（用于增量源优先级过滤）。"""
        if self.source == "tushare":
            return freq in ("d", "w", "m")
        if self.source == "baostock":
            return self.baostock.available(freq)
        if self.source == "sina":
            return SinaMinuteSource.available(freq)
        return True

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

    # ------------------------------------------------------------------ 行情
    def get_ohlc(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        freq: str = "d",
        *,
        adjust: str | None = None,
    ) -> pd.DataFrame:
        freq = normalize_frequency(freq)
        stock_code = normalize_code(stock_code)
        if not stock_code:
            raise DataNotFoundError("股票代码为空")
        if self.source == "akshare":
            frame = self.akshare.fetch_ohlc(stock_code, start_date, end_date, freq)
        elif self.source == "baostock":
            frame = self.baostock.fetch_ohlc(stock_code, start_date, end_date, freq)
        elif self.source == "tushare":
            frame = self.tushare.fetch_ohlc(stock_code, start_date, end_date, freq)
        elif self.source == "sina":
            frame = self.sina.fetch_ohlc(stock_code, start_date, end_date, freq)
        elif self.source == "questdb":
            frame = self._fetch_ohlc_from_questdb(stock_code, start_date, end_date, freq)
        else:
            raise ValueError(f"未知数据源: {self.source}")

        if adjust:
            frame = apply_adjust(
                frame, normalize_adjust(adjust), anchor=self.latest_adj_factor(stock_code)
            )
        return frame

    def _fetch_ohlc_from_questdb(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        table = ohlc_table_name(freq)
        base_fields = ["ts", "code", "open", "high", "low", "close", "volume", "amount"]
        if freq in ("d", "w", "m"):
            base_fields.append("turnover")
        query_end = end_date if " " in str(end_date) else f"{end_date} 23:59:59"
        sql = (
            f'SELECT {", ".join(base_fields + ["adj_factor"])} FROM "{table}" '
            "WHERE ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP) "
            "AND code = %s ORDER BY ts"
        )
        try:
            return self._read_sql_to_df(sql, (start_date, query_end, stock_code))
        except SourceUnavailableError:
            # 存量库尚未迁移（没有 adj_factor 列）：退化为不复权读取
            logger.warning("表 %s 缺少 adj_factor 列，请运行 scripts/migrate_database.py", table)
            sql = (
                f'SELECT {", ".join(base_fields)} FROM "{table}" '
                "WHERE ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP) "
                "AND code = %s ORDER BY ts"
            )
            frame = self._read_sql_to_df(sql, (start_date, query_end, stock_code))
            frame["adj_factor"] = pd.NA
            return frame

    def latest_adj_factor(self, stock_code: str) -> float | None:
        """该标的**最新**复权因子，作为 qfq 的锚点（跨查询区间稳定）。"""
        code = normalize_code(stock_code)
        cached = self._adj_cache.get(code)
        now = time.monotonic()
        if cached and now - cached[1] < _ADJ_FACTOR_TTL_SECONDS:
            return cached[0]
        value: float | None = None
        try:
            row = self.db.query(
                'SELECT adj_factor FROM "ohlcv_1d" WHERE code = %s '
                "LATEST ON ts PARTITION BY code",
                (code,),
                fetch_all=False,
            )
            if row and row[0] is not None:
                value = float(row[0])
        except Exception as exc:
            logger.debug("读取最新复权因子失败 %s: %s", code, exc)
        self._adj_cache[code] = (value, now)
        return value

    def get_realtime_quotes(
        self, ticker: str | None = None, source_type: str | None = None
    ):
        """单只返回 DataFrame；不传 ticker 时返回 ``{code: DataFrame}`` 快照。"""
        if ticker:
            return self.tencent.fetch_realtime(normalize_code(ticker))
        return self._fetch_spot_snapshot(source_type)

    def _fetch_spot_snapshot(self, source_type: str | None = None) -> dict[str, pd.DataFrame]:
        try:
            import akshare as ak
        except Exception as exc:  # akshare 未安装/导入失败
            raise SourceUnavailableError(f"AkShare 不可用: {exc}", source="akshare") from exc

        endpoints = {
            "stock": [ak.stock_zh_a_spot_em],
            # fund_etf_spot_ths 是同花顺“单位净值”表，不是行情快照，不能用于价格
            "etf": [getattr(ak, "fund_etf_spot_em", None)],
            "lof": [getattr(ak, "fund_lof_spot_em", None)],
        }
        targets = (
            endpoints.get(source_type)
            if source_type in endpoints
            else [func for funcs in endpoints.values() for func in funcs if func is not None]
        )
        # 列名映射：东财用“今开”，新浪用“开盘”，不同接口必须分别归一
        column_map = {
            "代码": "code",
            "最新价": "close",
            "今开": "open",
            "开盘": "open",
            "开盘价": "open",
            "最高": "high",
            "最高价": "high",
            "最低": "low",
            "最低价": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        result: dict[str, pd.DataFrame] = {}
        for func in targets:
            if func is None:
                continue
            try:
                frame = func()
            except Exception as exc:
                logger.warning("实时快照接口失败 %s: %s", getattr(func, "__name__", func), exc)
                continue
            if frame is None or frame.empty:
                continue
            renamed = frame.rename(columns=column_map)
            if "code" not in renamed.columns or "close" not in renamed.columns:
                logger.warning("实时快照缺少 code/close 列: %s", list(renamed.columns)[:8])
                continue
            renamed["code"] = renamed["code"].astype(str).map(normalize_code)
            for column in ("open", "high", "low", "close", "volume", "amount"):
                if column not in renamed.columns:
                    renamed[column] = pd.NA
            renamed = renamed.dropna(subset=["close"])
            stamp = pd.Timestamp(now_cn())
            for code, group in renamed.groupby("code"):
                if not code or code in result:
                    continue
                item = group.copy()
                item.index = pd.Index([stamp] * len(item), name="ts")
                result[code] = item[
                    ["code", "open", "high", "low", "close", "volume", "amount"]
                ]
        return result

    def get_ticker_type(self, ticker: str) -> str:
        return _ticker_type(ticker)

    # ------------------------------------------------------------------ 基础信息
    def get_stock_basic_info(self, code: str | None = None) -> pd.DataFrame:
        if self.source == "questdb":
            if code:
                sql = (
                    'SELECT * FROM "stock_basic_info" WHERE code = %s '
                    "LATEST ON ts PARTITION BY code"
                )
                return self._read_sql_to_df(sql, (normalize_code(code),))
            return self._read_sql_to_df(
                'SELECT * FROM "stock_basic_info" LATEST ON ts PARTITION BY code'
            )
        if self.source == "akshare":
            if not code:
                return pd.DataFrame()
            return self.akshare.fetch_stock_basic(code)
        return pd.DataFrame()

    def get_fund_basic_info(self, code: str | None = None) -> pd.DataFrame:
        if self.source == "questdb":
            if code:
                sql = (
                    'SELECT * FROM "fund_basic_info" WHERE code = %s '
                    "LATEST ON ts PARTITION BY code"
                )
                return self._read_sql_to_df(sql, (normalize_code(code),))
            return self._read_sql_to_df(
                'SELECT * FROM "fund_basic_info" LATEST ON ts PARTITION BY code'
            )
        if self.source == "akshare":
            if not code:
                return pd.DataFrame()
            return self.akshare.fetch_fund_basic(code)
        return pd.DataFrame()

    # ------------------------------------------------------------------ 概念板块
    def get_concept_boards(self) -> pd.DataFrame:
        if self.source == "questdb":
            return self._read_sql_to_df(
                'SELECT * FROM "concept_boards" LATEST ON ts PARTITION BY code'
            )
        return self.akshare.fetch_concept_boards()

    def get_concept_boards_ohlc(
        self, board_name: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        board_code = self.get_board_code_by_name(board_name)
        if self.source == "questdb":
            query_end = end_date if " " in str(end_date) else f"{end_date} 23:59:59"
            return self._read_sql_to_df(
                'SELECT * FROM "concept_boards_ohlc" '
                "WHERE ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP) "
                "AND code = %s ORDER BY ts",
                (start_date, query_end, board_code),
            )
        df = self.akshare.fetch_concept_board_ohlc(board_name, start_date, end_date)
        if not df.empty:
            df["code"] = board_code
        return df

    def get_concept_constituents(self, board_name: str) -> pd.DataFrame:
        board_code = self.get_board_code_by_name(board_name)
        if self.source == "questdb":
            return self._read_sql_to_df(
                'SELECT * FROM "concept_constituents" '
                "WHERE board_code = %s LATEST ON ts PARTITION BY stock_code",
                (board_code,),
            )
        df = self.akshare.fetch_concept_constituents(board_name)
        if not df.empty:
            df["board_code"] = board_code
        return df

    def get_board_code_by_name(self, board_name: str) -> str | None:
        try:
            row = self.db.query(
                'SELECT code FROM "concept_boards" WHERE name = %s '
                "LATEST ON ts PARTITION BY code",
                (board_name,),
                fetch_all=False,
            )
            if row and row[0]:
                return str(row[0])
        except Exception as exc:
            logger.debug("板块代码查询失败: %s", exc)

        df = self.akshare.fetch_concept_boards()
        if df is None or df.empty:
            raise DataNotFoundError(f"未找到板块: {board_name}", source="akshare")
        df = self._normalize_board_frame(df)
        self.update_concept_boards(df)
        matched = df[df["name"] == board_name]
        if matched.empty:
            raise DataNotFoundError(f"未找到板块: {board_name}", source="akshare")
        return str(matched["code"].iloc[0])

    @staticmethod
    def _normalize_board_frame(df: pd.DataFrame) -> pd.DataFrame:
        rename = {}
        for source_col in ("概念名称", "板块名称", "name"):
            if source_col in df.columns:
                rename[source_col] = "name"
                break
        for source_col in ("概念代码", "板块代码", "code"):
            if source_col in df.columns:
                rename[source_col] = "code"
                break
        return df.rename(columns=rename)

    # ------------------------------------------------------------------ 写入
    def update_ohlc(self, data: pd.DataFrame, freq: str) -> int:
        """写入不复权 OHLCV（含 adj_factor）。返回写入行数。"""
        if data is None or data.empty:
            return 0
        freq = normalize_frequency(freq)
        table = ohlc_table_name(freq)
        columns = OHLC_COLUMNS_MINUTE if freq.endswith("min") else OHLC_COLUMNS_DAILY

        rows: list[tuple] = []
        skipped = 0
        for index, row in data.iterrows():
            ts_value = row.get("ts") if "ts" in data.columns else index
            prices = [_to_float(row.get(column)) for column in ("open", "high", "low", "close")]
            if ts_value is None or any(price is None for price in prices):
                skipped += 1
                continue
            values: list[Any] = [_ts_to_text(ts_value), *prices]
            values.extend(
                [
                    _to_int(row.get("volume")) or 0,
                    _to_float(row.get("amount")) or 0.0,
                ]
            )
            if not freq.endswith("min"):
                values.append(_to_float(row.get("turnover")))
            values.append(_to_float(row.get("adj_factor")))
            values.append(normalize_code(row.get("code")))
            values.append(MARKET_CN)
            rows.append(tuple(values))

        if skipped:
            logger.warning("%s 有 %s 行因 OHLC 缺失被跳过", table, skipped)
        if not rows:
            return 0
        quoted = ", ".join(f'"{column}"' for column in columns)
        prefix = f'INSERT INTO "{table}" ({quoted}) VALUES %s'
        return self.db.execute_values(prefix, rows)

    def update_stock_basic(self, data: pd.DataFrame) -> int:
        """写 ``stock_basic_info``。

        列集合与 financial 层共用 ``PROFILE_COLUMNS``：此前 market 侧自造了两个
        DDL 里不存在的列名（主营业务/所属行业），导致每次 INSERT 都失败且被吞掉。
        """
        if data is None or data.empty:
            return 0
        from data_interface.financial.fields import PROFILE_COLUMNS
        from data_interface.financial.normalizer import row_to_values

        rows = []
        for index, row in data.iterrows():
            code = normalize_code(row.get("code"))
            record = {column: None for column in PROFILE_COLUMNS}
            record.update(
                {
                    "ts": row.get("ts") if "ts" in data.columns and pd.notna(row.get("ts")) else now_cn(),
                    "code": code,
                    "source": row.get("source", "akshare"),
                    "updated_at": now_cn(),
                }
            )
            for column in PROFILE_COLUMNS:
                if column in ("ts", "code", "source", "updated_at"):
                    continue
                if column in row and pd.notna(row[column]):
                    record[column] = row[column]

            # market 侧只抓到部分字段；不能把 financial profile 已有的
            # area/delist_date/sw_* 等覆盖成 NULL（LATEST ON ts 会读到空值）。
            existing: dict[str, Any] = {}
            if code:
                try:
                    frame = self._read_sql_to_df(
                        'SELECT * FROM "stock_basic_info" WHERE code = %s '
                        "LATEST ON ts PARTITION BY code",
                        (code,),
                    )
                    if not frame.empty:
                        existing = frame.iloc[0].to_dict()
                except SourceUnavailableError:
                    # 读失败时宁可让写入失败，也不要把完整 profile 写成空
                    raise
            for column in PROFILE_COLUMNS:
                if column in ("ts", "code", "source", "updated_at"):
                    continue
                if record.get(column) is None and existing.get(column) is not None:
                    value = existing.get(column)
                    try:
                        if pd.notna(value):
                            record[column] = value
                    except (TypeError, ValueError):
                        record[column] = value
            # market 快照无法判断退市状态；保留已有的 D/P，避免“复活”退市股
            if existing.get("list_status") in ("D", "P"):
                record["list_status"] = existing["list_status"]
            if not record.get("market"):
                record["market"] = "A"
            rows.append(row_to_values(record, PROFILE_COLUMNS))
        if not rows:
            return 0
        prefix = (
            f'INSERT INTO "stock_basic_info" '
            f'({", ".join(chr(34) + c + chr(34) for c in PROFILE_COLUMNS)}) VALUES %s'
        )
        return self.db.execute_values(prefix, rows)

    def update_fund_basic(self, data: pd.DataFrame) -> int:
        if data is None or data.empty:
            return 0
        columns = [
            "ts",
            "code",
            "name",
            "fullname",
            "found_date",
            "total_asset",
            "management",
            "fund_type",
            "rating_agency",
            "rating",
        ]
        rows = []
        for index, row in data.iterrows():
            ts_value = row.get("ts") if "ts" in data.columns else index
            rows.append(
                (
                    _ts_to_text(ts_value if ts_value is not None else now_cn()),
                    normalize_code(row.get("code")),
                    _str_or_none(row.get("name")),
                    _str_or_none(row.get("fullname")),
                    row.get("found_date") if pd.notna(row.get("found_date")) else None,
                    _str_or_none(row.get("total_asset")),
                    _str_or_none(row.get("management")),
                    _str_or_none(row.get("fund_type")),
                    _str_or_none(row.get("rating_agency")),
                    _str_or_none(row.get("rating")),
                )
            )
        prefix = (
            f'INSERT INTO "fund_basic_info" '
            f'({", ".join(chr(34) + c + chr(34) for c in columns)}) VALUES %s'
        )
        return self.db.execute_values(prefix, rows)

    def update_concept_boards(self, data: pd.DataFrame) -> int:
        if data is None or data.empty:
            return 0
        data = self._normalize_board_frame(data)
        rows = [
            (
                _ts_to_text(row.get("ts") if "ts" in data.columns else index),
                str(row.get("code")),
                _str_or_none(row.get("name")),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_values(
            'INSERT INTO "concept_boards" (ts, code, name) VALUES %s', rows
        )

    def update_concept_boards_ohlc(self, data: pd.DataFrame) -> int:
        if data is None or data.empty:
            return 0
        rows = [
            (
                _ts_to_text(row.get("ts") if "ts" in data.columns else index),
                str(row.get("code")),
                _str_or_none(row.get("name")),
                _to_float(row.get("open")),
                _to_float(row.get("high")),
                _to_float(row.get("low")),
                _to_float(row.get("close")),
                _to_float(row.get("volume")),
                _to_float(row.get("amount")),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_values(
            'INSERT INTO "concept_boards_ohlc" '
            "(ts, code, name, open, high, low, close, volume, amount) VALUES %s",
            rows,
        )

    def update_concept_constituents(self, data: pd.DataFrame) -> int:
        if data is None or data.empty:
            return 0
        rows = [
            (
                _ts_to_text(row.get("ts") if "ts" in data.columns else index),
                _str_or_none(row.get("board_name")),
                str(row.get("board_code")),
                _str_or_none(row.get("stock_name")),
                str(row.get("stock_code")),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_values(
            'INSERT INTO "concept_constituents" '
            "(ts, board_name, board_code, stock_name, stock_code) VALUES %s",
            rows,
        )

    def _read_sql_to_df(self, sql: str, params: tuple | None = None) -> pd.DataFrame:
        """读库失败必须抛出，不能把故障伪装成「无数据」。"""
        try:
            return self.db.query_dataframe(sql, params)
        except Exception as exc:
            logger.error("QuestDB 查询失败: %s | SQL=%s", exc, sql[:200])
            raise SourceUnavailableError(
                f"QuestDB 查询失败: {exc}", source="questdb"
            ) from exc


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None
