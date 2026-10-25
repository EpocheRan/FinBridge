"""行情数据获取器。

负责多源调度与 QuestDB 读写；具体第三方接口细节放在 ``sources`` 下。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient

from .constants import ohlc_table_name
from .sources import AkshareMarketSource, BaoStockMarketSource, TencentQuoteSource

logger = logging.getLogger("DataInterface.MarketFetcher")


def normalize_code(code: str) -> str:
    return code.split(".")[0] if isinstance(code, str) else str(code)


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
    ):
        self.config = config or get_config()
        self.source = source
        self.db = db or QuestDBClient(self.config)
        self.akshare = akshare or AkshareMarketSource(self.config)
        self.baostock = baostock or BaoStockMarketSource()
        self.tencent = tencent or TencentQuoteSource(
            int(self.config["market"].get("request_timeout_seconds", 8))
        )

    # ------------------------------------------------------------------ 行情
    def get_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str = "d"
    ) -> pd.DataFrame:
        stock_code = normalize_code(stock_code)
        if self.source == "akshare":
            return self.akshare.fetch_ohlc(stock_code, start_date, end_date, freq)
        if self.source == "baostock":
            return self.baostock.fetch_ohlc(stock_code, start_date, end_date, freq)
        if self.source == "questdb":
            return self._fetch_ohlc_from_questdb(stock_code, start_date, end_date, freq)
        raise ValueError(f"未知数据源: {self.source}")

    def _fetch_ohlc_from_questdb(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        table = ohlc_table_name(freq)
        fields = (
            "ts, code, open, high, low, close, volume, amount, turnover"
            if freq in ("d", "w", "m")
            else "ts, code, open, high, low, close, volume, amount"
        )
        return self._read_sql_to_df(
            f'SELECT {fields} FROM "{table}" '
            f"WHERE ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP) "
            "AND code = %s ORDER BY ts",
            (start_date, end_date, stock_code),
        )

    def get_realtime_quotes(self, ticker: str | None = None, source_type: str | None = None):
        if ticker:
            return self.tencent.fetch_realtime(normalize_code(ticker))
        return self._fetch_spot_snapshot(source_type)

    def _fetch_spot_snapshot(self, source_type: str | None = None) -> dict:
        try:
            import akshare as ak
        except ImportError:
            return {}

        sources = {
            "stock": ak.stock_zh_a_spot_em,
            "etf": ak.fund_etf_spot_ths,
            "lof": ak.stock_zh_a_spot,
        }
        frames = []
        if source_type in sources:
            candidates = [sources[source_type]]
        else:
            candidates = list(sources.values())
        for func in candidates:
            try:
                df = func()
                if not df.empty:
                    frames.append(df)
            except Exception:
                continue
        if not frames:
            return {}

        combined = pd.concat(frames)
        output = pd.DataFrame()
        output["code"] = combined["代码"] if "代码" in combined.columns else []
        mapping = {
            "最新价": "close",
            "开盘": "open",
            "开盘价": "open",
            "最高": "high",
            "最高价": "high",
            "最低": "low",
            "最低价": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        for source_col, target_col in mapping.items():
            if source_col in combined.columns:
                output[target_col] = combined[source_col]
        output["ts"] = pd.Timestamp.now()

        result = {}
        for code, group in output.groupby("code"):
            frame = group.copy()
            frame.set_index("ts", inplace=True)
            result[code] = frame[
                ["code", "open", "high", "low", "close", "volume", "amount"]
            ]
        return result

    def get_ticker_type(self, ticker: str) -> str:
        return self.akshare.get_ticker_type(normalize_code(ticker))

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
            return self._read_sql_to_df(
                'SELECT * FROM "concept_boards_ohlc" '
                "WHERE ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP) "
                "AND code = %s ORDER BY ts",
                (start_date, end_date, board_code),
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
        rows = self.db.query(
            'SELECT code FROM "concept_boards" WHERE name = %s '
            "LATEST ON ts PARTITION BY code",
            (board_name,),
            fetch_all=False,
        )
        if rows:
            return rows[0]
        df = self.akshare.fetch_concept_boards()
        if df.empty:
            return None
        df = self._normalize_board_frame(df)
        self.update_concept_boards(df)
        matched = df[df["name"] == board_name]
        return str(matched["code"].iloc[0]) if not matched.empty else None

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
        if data.empty:
            return 0
        table = ohlc_table_name(freq)
        if freq.endswith("min"):
            sql = (
                f'INSERT INTO "{table}" '
                "(ts, open, high, low, close, volume, amount, code, market) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'MLCN')"
            )
            rows = [
                (
                    pd.Timestamp(row["ts"] if "ts" in data.columns else index).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["volume"]),
                    float(row["amount"]),
                    str(row["code"]),
                )
                for index, row in data.iterrows()
            ]
        else:
            sql = (
                f'INSERT INTO "{table}" '
                "(ts, open, high, low, close, volume, amount, turnover, code, market) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'MLCN')"
            )
            rows = [
                (
                    pd.Timestamp(row["ts"] if "ts" in data.columns else index).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["volume"]),
                    float(row["amount"]),
                    float(row.get("turnover", 0) or 0),
                    str(row["code"]),
                )
                for index, row in data.iterrows()
            ]
        return self.db.execute_many(sql, rows)

    def update_stock_basic(self, data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        sql = (
            "INSERT INTO stock_basic_info "
            "(ts, code, name, fullname, main_operation_biz, affiliate_industry, "
            "total_share, float_share, total_mv, float_mv, list_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        rows = []
        for index, row in data.iterrows():
            ts = row.get("ts") if "ts" in data.columns else index
            ts = pd.Timestamp(ts)
            rows.append(
                (
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    str(row["code"]),
                    str(row.get("name")) if pd.notna(row.get("name")) else None,
                    str(row.get("fullname")) if pd.notna(row.get("fullname")) else None,
                    str(row.get("main_operation_biz")) if pd.notna(row.get("main_operation_biz")) else None,
                    str(row.get("affiliate_industry")) if pd.notna(row.get("affiliate_industry")) else None,
                    float(row.get("total_share")) if pd.notna(row.get("total_share")) else None,
                    float(row.get("float_share")) if pd.notna(row.get("float_share")) else None,
                    float(row.get("total_mv")) if pd.notna(row.get("total_mv")) else None,
                    float(row.get("float_mv")) if pd.notna(row.get("float_mv")) else None,
                    row.get("list_date") if pd.notna(row.get("list_date")) else None,
                )
            )
        return self.db.execute_many(sql, rows)

    def update_fund_basic(self, data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        sql = (
            "INSERT INTO fund_basic_info "
            "(ts, code, name, fullname, found_date, total_asset, management, "
            "fund_type, rating_agency, rating) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        rows = []
        for index, row in data.iterrows():
            ts = row.get("ts") if "ts" in data.columns else index
            ts = pd.Timestamp(ts)
            rows.append(
                (
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    str(row["code"]),
                    str(row.get("name")) if pd.notna(row.get("name")) else None,
                    str(row.get("fullname")) if pd.notna(row.get("fullname")) else None,
                    row.get("found_date") if pd.notna(row.get("found_date")) else None,
                    str(row.get("total_asset")) if pd.notna(row.get("total_asset")) else None,
                    str(row.get("management")) if pd.notna(row.get("management")) else None,
                    str(row.get("fund_type")) if pd.notna(row.get("fund_type")) else None,
                    str(row.get("rating_agency")) if pd.notna(row.get("rating_agency")) else None,
                    str(row.get("rating")) if pd.notna(row.get("rating")) else None,
                )
            )
        return self.db.execute_many(sql, rows)

    def update_concept_boards(self, data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        data = self._normalize_board_frame(data)
        sql = 'INSERT INTO "concept_boards" (ts, code, name) VALUES (%s, %s, %s)'
        rows = [
            (
                pd.Timestamp(row["ts"] if "ts" in data.columns else index).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                str(row["code"]),
                str(row["name"]),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_many(sql, rows)

    def update_concept_boards_ohlc(self, data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        sql = (
            'INSERT INTO "concept_boards_ohlc" '
            "(ts, code, name, open, high, low, close, volume, amount) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        rows = [
            (
                pd.Timestamp(row["ts"] if "ts" in data.columns else index).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                str(row["code"]),
                str(row["name"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
                float(row["amount"]),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_many(sql, rows)

    def update_concept_constituents(self, data: pd.DataFrame) -> int:
        if data.empty:
            return 0
        sql = (
            'INSERT INTO "concept_constituents" '
            "(ts, board_name, board_code, stock_name, stock_code) "
            "VALUES (%s, %s, %s, %s, %s)"
        )
        rows = [
            (
                pd.Timestamp(row["ts"] if "ts" in data.columns else index).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                str(row["board_name"]),
                str(row["board_code"]),
                str(row["stock_name"]),
                str(row["stock_code"]),
            )
            for index, row in data.iterrows()
        ]
        return self.db.execute_many(sql, rows)

    def _read_sql_to_df(self, sql: str, params: tuple | None = None) -> pd.DataFrame:
        try:
            df = self.db.query_dataframe(sql, params)
            return df if df is not None else pd.DataFrame()
        except Exception as exc:
            logger.error("QuestDB 查询失败: %s | SQL=%s", exc, sql[:200])
            return pd.DataFrame()
