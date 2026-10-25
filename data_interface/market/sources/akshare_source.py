from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger("DataInterface.AkShareMarket")


def _normalize_code(code: str) -> str:
    return code.split(".")[0] if isinstance(code, str) else str(code)


class AkshareMarketSource:
    """AkShare 行情/基础信息/概念板块适配器。"""

    name = "akshare"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @staticmethod
    def _ak():
        import akshare as ak

        return ak

    @staticmethod
    def get_ticker_type(ticker: str) -> str:
        if ticker.startswith(("51", "15", "50")):
            return "etf"
        if ticker.startswith(("16",)):
            return "lof"
        return "stock"

    # ------------------------------------------------------------------ OHLC
    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        ak = self._ak()
        stock_code = _normalize_code(stock_code)
        ticker_type = self.get_ticker_type(stock_code)

        if freq.endswith("min"):
            period = int(freq.replace("min", ""))
            if ticker_type in ["etf", "lof"]:
                prefix = (
                    "sz"
                    if stock_code.startswith(("15", "16", "18", "39", "159"))
                    else "sh"
                )
                try:
                    df = ak.stock_zh_a_minute(
                        symbol=f"{prefix}{stock_code}", period=f"{period}", adjust="qfq"
                    )
                    if df.empty:
                        return pd.DataFrame()
                    df.columns = ["ts", "open", "high", "low", "close", "volume"]
                    df["amount"] = 0
                    df["ts"] = pd.to_datetime(df["ts"])
                    df["code"] = stock_code
                    start_dt = pd.to_datetime(start_date)
                    end_dt = pd.to_datetime(end_date) + timedelta(days=1)
                    df = df[(df["ts"] >= start_dt) & (df["ts"] < end_dt)]
                    df.set_index("ts", inplace=True)
                    return df[
                        ["code", "open", "high", "low", "close", "volume", "amount"]
                    ]
                except Exception as exc:
                    logger.warning("新浪分钟行情失败 %s: %s", stock_code, exc)
                    return pd.DataFrame()

            func = ak.stock_zh_a_hist_min_em
            all_data = []
            curr = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            while curr <= end_dt:
                chunk_end = min(curr + timedelta(days=7), end_dt)
                try:
                    df = func(
                        symbol=stock_code,
                        period=f"{period}",
                        start_date=curr.strftime("%Y%m%d"),
                        end_date=chunk_end.strftime("%Y%m%d"),
                        adjust="qfq",
                    )
                    if not df.empty:
                        all_data.append(df)
                except Exception:
                    pass
                curr = chunk_end + timedelta(days=1)
            if not all_data:
                return pd.DataFrame()
            df = pd.concat(all_data)
            df.columns = (
                ["ts", "open", "high", "low", "close", "volume", "amount", "dummy1", "dummy2"]
                if len(df.columns) == 9
                else ["ts", "open", "high", "low", "close", "volume", "amount"]
            )
            df["ts"] = pd.to_datetime(df["ts"])
            df["code"] = stock_code
            df.set_index("ts", inplace=True)
            return df[["code", "open", "high", "low", "close", "volume", "amount"]]

        period_map = {"d": "daily", "w": "weekly", "m": "monthly"}
        start_compact = start_date.replace("-", "")
        end_compact = end_date.replace("-", "")

        if ticker_type in ["etf", "lof"]:
            prefix = (
                "sz" if stock_code.startswith(("15", "16", "18", "39", "159")) else "sh"
            )
            try:
                df = ak.fund_etf_hist_sina(symbol=f"{prefix}{stock_code}")
                if df.empty:
                    return pd.DataFrame()
                df["date"] = pd.to_datetime(df["date"])
                start_dt = pd.to_datetime(start_date)
                end_dt = pd.to_datetime(end_date)
                df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
                df.rename(
                    columns={
                        "date": "日期",
                        "open": "开盘",
                        "high": "最高",
                        "low": "最低",
                        "close": "收盘",
                        "volume": "成交量",
                        "amount": "成交额",
                    },
                    inplace=True,
                )
                if "换手率" not in df.columns:
                    df["换手率"] = 0
            except Exception as exc:
                logger.warning("新浪基金行情失败 %s: %s", stock_code, exc)
                return pd.DataFrame()
        else:
            prefix = "sh" if stock_code.startswith("6") else "sz"
            if stock_code.startswith(("4", "8")):
                prefix = "bj"
            try:
                df = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{stock_code}",
                    start_date=start_compact,
                    end_date=end_compact,
                    adjust="qfq",
                )
                if df.empty:
                    return pd.DataFrame()
                df.rename(
                    columns={
                        "date": "日期",
                        "open": "开盘",
                        "high": "最高",
                        "low": "最低",
                        "close": "收盘",
                        "volume": "成交量",
                        "amount": "成交额",
                        "turnover": "换手率",
                    },
                    inplace=True,
                )
                if "换手率" not in df.columns:
                    df["换手率"] = 0
            except Exception as exc:
                logger.warning("新浪股票行情失败 %s: %s", stock_code, exc)
                return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()
        df = df[
            ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率"]
        ]
        df.columns = [
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover",
        ]
        df["ts"] = pd.to_datetime(df["ts"])
        df["code"] = stock_code
        df.set_index("ts", inplace=True)
        return df[
            ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
        ]

    # ------------------------------------------------------------------ 基础信息
    def fetch_stock_basic(self, code: str) -> pd.DataFrame:
        ak = self._ak()
        code = _normalize_code(code)
        try:
            df_em = ak.stock_individual_info_em(symbol=code)
            em_map = dict(zip(df_em["item"], df_em["value"]))

            xq_code = code
            if code.startswith("6"):
                xq_code = f"SH{code}"
            elif code.startswith(("0", "3")):
                xq_code = f"SZ{code}"
            elif code.startswith(("4", "8")):
                xq_code = f"BJ{code}"

            df_xq = ak.stock_individual_basic_info_xq(symbol=xq_code)
            main_biz = affiliate_ind = short_name = fullname = None
            if not df_xq.empty and "item" in df_xq.columns and "value" in df_xq.columns:
                xq_map = dict(zip(df_xq["item"], df_xq["value"]))
                main_biz = xq_map.get("main_operation_business")
                affiliate_ind = xq_map.get("affiliate_industry")
                short_name = xq_map.get("org_short_name_cn")
                fullname = xq_map.get("org_name_cn")

            data = {
                "ts": pd.Timestamp.now(),
                "code": code,
                "name": short_name,
                "fullname": fullname,
                "main_operation_biz": main_biz,
                "affiliate_industry": affiliate_ind,
                "total_share": pd.to_numeric(em_map.get("总股本"), errors="coerce"),
                "float_share": pd.to_numeric(em_map.get("流通股"), errors="coerce"),
                "total_mv": pd.to_numeric(em_map.get("总市值"), errors="coerce"),
                "float_mv": pd.to_numeric(em_map.get("流通市值"), errors="coerce"),
                "list_date": pd.to_datetime(em_map.get("上市时间"), errors="coerce"),
            }
            return pd.DataFrame([data])
        except Exception as exc:
            logger.error("AkShare 股票基础信息失败 %s: %s", code, exc)
            return pd.DataFrame()

    def fetch_fund_basic(self, code: str) -> pd.DataFrame:
        ak = self._ak()
        code = _normalize_code(code)
        values: dict = {}
        try:
            df = ak.fund_individual_basic_info_xq(symbol=code)
            if not df.empty and "item" in df.columns and "value" in df.columns:
                xq_map = dict(zip(df["item"], df["value"]))
                values.update(
                    {
                        "name": xq_map.get("基金名称"),
                        "fullname": xq_map.get("基金全称"),
                        "found_date": xq_map.get("成立时间"),
                        "total_asset": xq_map.get("最新规模"),
                        "management": xq_map.get("基金公司"),
                        "fund_type": xq_map.get("基金类型"),
                        "rating_agency": xq_map.get("评级机构"),
                        "rating": xq_map.get("基金评级"),
                    }
                )
        except Exception as exc:
            logger.warning("雪球基金信息失败 %s: %s", code, exc)

        if not values:
            try:
                if code.startswith(("15", "16", "18", "39")):
                    df_szse = ak.fund_etf_scale_szse()
                    match = df_szse[df_szse["基金代码"].astype(str) == code]
                    if not match.empty:
                        row = match.iloc[0]
                        values.update(
                            {
                                "name": row.get("基金简称"),
                                "fullname": row.get("基金简称"),
                                "total_asset": str(row.get("基金份额")),
                                "fund_type": "ETF",
                            }
                        )
                elif code.startswith(("51", "58", "68", "50")):
                    df_sse = ak.fund_etf_scale_sse()
                    match = df_sse[df_sse["基金代码"] == code]
                    if not match.empty:
                        row = match.iloc[0]
                        values.update(
                            {
                                "name": row.get("基金简称"),
                                "fullname": row.get("基金简称"),
                                "fund_type": "ETF",
                            }
                        )
            except Exception as exc:
                logger.warning("交易所基金信息失败 %s: %s", code, exc)

        if not values:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "ts": pd.Timestamp.now(),
                    "code": code,
                    "name": values.get("name"),
                    "fullname": values.get("fullname"),
                    "found_date": pd.to_datetime(values.get("found_date"), errors="coerce"),
                    "total_asset": str(values.get("total_asset")) if values.get("total_asset") else None,
                    "management": values.get("management"),
                    "fund_type": values.get("fund_type"),
                    "rating_agency": values.get("rating_agency"),
                    "rating": str(values.get("rating")) if values.get("rating") else None,
                }
            ]
        )

    # ------------------------------------------------------------------ 概念板块
    def fetch_concept_boards(self) -> pd.DataFrame:
        ak = self._ak()
        try:
            df = ak.stock_board_concept_name_ths()
            df["ts"] = pd.Timestamp.today().date()
            df.set_index("ts", inplace=True)
            return df
        except Exception as exc:
            logger.warning("概念板块列表失败: %s", exc)
            return pd.DataFrame()

    def fetch_concept_board_ohlc(
        self, board_name: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        ak = self._ak()
        try:
            df = ak.stock_board_concept_index_ths(
                symbol=board_name, start_date=start_date, end_date=end_date
            )
            df.columns = ["ts", "open", "high", "low", "close", "volume", "amount"]
            df["ts"] = pd.to_datetime(df["ts"])
            df["name"] = board_name
            df.set_index("ts", inplace=True)
            return df
        except Exception as exc:
            logger.warning("概念板块行情失败 %s: %s", board_name, exc)
            return pd.DataFrame()

    def fetch_concept_constituents(self, board_name: str) -> pd.DataFrame:
        ak = self._ak()
        try:
            df = ak.stock_board_concept_cons_em(symbol=board_name)
            rename = {}
            for source_col in ("股票代码", "代码"):
                if source_col in df.columns:
                    rename[source_col] = "stock_code"
                    break
            for source_col in ("股票名称", "名称"):
                if source_col in df.columns:
                    rename[source_col] = "stock_name"
                    break
            df = df.rename(columns=rename)
            if "stock_code" not in df.columns:
                return pd.DataFrame()
            if "stock_name" not in df.columns:
                df["stock_name"] = df["stock_code"]
            df["ts"] = pd.Timestamp.today().date()
            df["board_name"] = board_name
            return df
        except Exception as exc:
            logger.warning("概念板块成分失败 %s: %s", board_name, exc)
            return pd.DataFrame()
