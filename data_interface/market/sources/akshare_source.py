"""AkShare 行情/基础信息/概念板块适配器。

约定：

- **只返回不复权价**（``adjust=""``），复权因子由 ``AdjFactorProvider`` 单独取；
- 成交量统一为「股」、成交额统一为「元」、换手率统一为「%」；
- 列映射按中文列名 rename，不再用「列数等于 7/9/11」这种脆弱的硬编码；
- 源层失败返回空表并记录 warning，由上层决定是否降级/报告。
"""

from __future__ import annotations

import logging

import pandas as pd

from data_interface.common.security_code import (
    is_fund_type,
    normalize_code,
    ticker_type as _ticker_type,
)
from data_interface.common.timeutil import now_cn

logger = logging.getLogger("DataInterface.AkShareMarket")

# 兼容旧调用方
_normalize_code = normalize_code

DAILY_PERIOD = {"d": "daily", "w": "weekly", "m": "monthly"}
# ts 会作为索引，不参与最终列选择；code 必须保留（写入端按 code 分组）
MINUTE_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "amount", "code")
DAILY_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "amount", "turnover", "code")

# 东财/新浪/同花顺的中文列名 -> canonical
_COLUMN_ALIASES = {
    "日期": "ts",
    "时间": "ts",
    "date": "ts",
    "trade_date": "ts",
    "开盘": "open",
    "今开": "open",
    "收盘": "close",
    "最新价": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
    "涨跌幅": "change_pct",
    "振幅": "amplitude",
}


def _rename_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={k: v for k, v in _COLUMN_ALIASES.items() if k in frame.columns})


def _require(frame: pd.DataFrame, columns: tuple[str, ...]) -> bool:
    return all(column in frame.columns for column in columns)


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
        return _ticker_type(ticker)

    # ------------------------------------------------------------------ OHLC
    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        ak = self._ak()
        stock_code = normalize_code(stock_code)
        ticker_type = self.get_ticker_type(stock_code)

        if freq.endswith("min"):
            frame = self._fetch_minute(ak, stock_code, ticker_type, start_date, end_date, freq)
        else:
            frame = self._fetch_daily(ak, stock_code, ticker_type, start_date, end_date, freq)

        if frame is None or frame.empty:
            return pd.DataFrame()
        return frame

    # ---------------------------------------------------------- 分钟线（股票/基金）
    def _fetch_minute(
        self, ak, code: str, ticker_type: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        period = int(freq.replace("min", ""))
        start_at = f"{start_date} 09:30:00"
        end_at = f"{end_date} 15:00:00"

        if is_fund_type(ticker_type):
            func = ak.fund_etf_hist_min_em if ticker_type == "etf" else ak.fund_lof_hist_min_em
            try:
                raw = func(
                    symbol=code,
                    period=str(period),
                    adjust="",
                    start_date=start_at,
                    end_date=end_at,
                )
            except Exception as exc:
                logger.warning("东财基金分钟行情失败 %s: %s", code, exc)
                return pd.DataFrame()
            frame = self._standardize_minute(raw, code, volume_in_lots=True)
        else:
            try:
                raw = ak.stock_zh_a_hist_min_em(
                    symbol=code,
                    period=str(period),
                    start_date=start_at,
                    end_date=end_at,
                    adjust="",
                )
            except Exception as exc:
                logger.warning("东财股票分钟行情失败 %s: %s", code, exc)
                return pd.DataFrame()
            frame = self._standardize_minute(raw, code, volume_in_lots=True)

        if frame.empty:
            return frame
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        return frame[(frame.index >= start_dt) & (frame.index < end_dt)]

    def _standardize_minute(
        self, raw: pd.DataFrame, code: str, *, volume_in_lots: bool
    ) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame()
        frame = _rename_columns(raw.copy())
        if not _require(frame, ("ts", "open", "close")):
            logger.warning("分钟行情列名无法识别 %s: %s", code, list(raw.columns)[:10])
            return pd.DataFrame()
        if "high" not in frame.columns:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        if "low" not in frame.columns:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column not in frame.columns:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        # 东财分钟成交量单位是「手」（1 手 = 100 股）
        if volume_in_lots:
            frame["volume"] = frame["volume"].fillna(0) * 100
        frame["amount"] = frame["amount"].fillna(0)
        frame["code"] = code
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
        frame = frame.set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        # ts 已经是索引，不能再当列选
        return frame[[column for column in MINUTE_COLUMNS if column != "ts"]]

    # ---------------------------------------------------------- 日/周/月线
    def _fetch_daily(
        self, ak, code: str, ticker_type: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        period = DAILY_PERIOD[freq]
        start_compact = start_date.replace("-", "")
        end_compact = end_date.replace("-", "")

        if is_fund_type(ticker_type):
            frame = self._fetch_fund_daily(
                ak, code, ticker_type, period, start_compact, end_compact, freq
            )
        else:
            frame = self._fetch_stock_daily(ak, code, period, start_compact, end_compact)
        return frame

    def _fetch_stock_daily(
        self, ak, code: str, period: str, start_compact: str, end_compact: str
    ) -> pd.DataFrame:
        # stock_zh_a_hist 支持 daily/weekly/monthly；stock_zh_a_daily 只有日线（旧代码的 bug）
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period=period,
                start_date=start_compact,
                end_date=end_compact,
                adjust="",
            )
        except Exception as exc:
            logger.warning("东财股票行情失败 %s(%s): %s", code, period, exc)
            return pd.DataFrame()
        return self._standardize_daily(raw, code, volume_in_lots=True)

    def _fetch_fund_daily(
        self,
        ak,
        code: str,
        ticker_type: str,
        period: str,
        start_compact: str,
        end_compact: str,
        freq: str,
    ) -> pd.DataFrame:
        func = ak.fund_etf_hist_em if ticker_type == "etf" else ak.fund_lof_hist_em
        frame = pd.DataFrame()
        try:
            raw = func(
                symbol=code,
                period=period,
                start_date=start_compact,
                end_date=end_compact,
                adjust="",
            )
            frame = self._standardize_daily(raw, code, volume_in_lots=True)
        except Exception as exc:
            logger.warning("东财基金日线失败 %s: %s", code, exc)

        if frame.empty and freq == "d":
            frame = self._fetch_fund_daily_sina(ak, code, start_compact, end_compact)
        return frame

    def _fetch_fund_daily_sina(
        self, ak, code: str, start_compact: str, end_compact: str
    ) -> pd.DataFrame:
        prefix = "sz" if code.startswith(("15", "16", "18", "39")) else "sh"
        try:
            raw = ak.fund_etf_hist_sina(symbol=f"{prefix}{code}")
        except Exception as exc:
            logger.warning("新浪基金行情失败 %s: %s", code, exc)
            return pd.DataFrame()
        if raw is None or raw.empty:
            return pd.DataFrame()
        frame = _rename_columns(raw.copy())
        if not _require(frame, ("ts", "open", "close")):
            return pd.DataFrame()
        # 新浪不提供成交额，用 收盘 × 成交量 估算，避免 KeyError
        if "amount" not in frame.columns:
            frame["amount"] = pd.to_numeric(frame["close"], errors="coerce") * pd.to_numeric(
                frame.get("volume", 0), errors="coerce"
            )
        if "turnover" not in frame.columns:
            frame["turnover"] = pd.NA
        frame = self._standardize_daily(frame, code, volume_in_lots=False)
        if frame.empty:
            return frame
        start_dt = pd.to_datetime(start_compact)
        end_dt = pd.to_datetime(end_compact)
        return frame[(frame.index >= start_dt) & (frame.index <= end_dt)]

    def _standardize_daily(
        self, raw: pd.DataFrame, code: str, *, volume_in_lots: bool
    ) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame()
        frame = _rename_columns(raw.copy())
        if not _require(frame, ("ts", "open", "close")):
            logger.warning("日线列名无法识别 %s: %s", code, list(raw.columns)[:10])
            return pd.DataFrame()
        if "high" not in frame.columns:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        if "low" not in frame.columns:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        for column in ("open", "high", "low", "close", "volume", "amount", "turnover"):
            if column not in frame.columns:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if volume_in_lots:
            frame["volume"] = frame["volume"].fillna(0) * 100
        frame["amount"] = frame["amount"].fillna(0)
        frame["code"] = code
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
        frame = frame.set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame[[column for column in DAILY_COLUMNS if column != "ts"]]

    # ------------------------------------------------------------------ 复权因子
    def fetch_adj_factor(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """新浪后复权因子（仅股票）。返回 ``trade_date, adj_factor``。"""
        ak = self._ak()
        code = normalize_code(stock_code)
        prefix = "bj" if code.startswith(("4", "8", "92")) else (
            "sh" if code.startswith("6") else "sz"
        )
        try:
            raw = ak.stock_zh_a_daily(
                symbol=f"{prefix}{code}",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="hfq-factor",
            )
        except Exception as exc:
            logger.warning("新浪复权因子失败 %s: %s", code, exc)
            return pd.DataFrame()
        if raw is None or raw.empty:
            return pd.DataFrame()
        frame = raw.copy()
        rename = {}
        for column in frame.columns:
            lower = str(column).lower()
            if lower in ("date", "trade_date"):
                rename[column] = "trade_date"
            if "factor" in lower:
                rename[column] = "adj_factor"
        frame = frame.rename(columns=rename)
        if "trade_date" not in frame.columns or "adj_factor" not in frame.columns:
            return pd.DataFrame()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
        return frame.dropna(subset=["trade_date", "adj_factor"])[["trade_date", "adj_factor"]]

    # ------------------------------------------------------------------ 基础信息
    def fetch_stock_basic(self, code: str) -> pd.DataFrame:
        """返回与 ``stock_basic_info`` DDL 同名的列，供 market/financial 共用写入。"""
        ak = self._ak()
        code = normalize_code(code)
        info: dict = {
            "ts": now_cn(),
            "code": code,
            "exchange": None,
            "board": None,
            "market": "A",
            "security_type": "STOCK",
            "currency": "CNY",
            "list_status": "L",
            "source": "akshare",
        }
        try:
            em = ak.stock_individual_info_em(symbol=code)
            em_map = dict(zip(em["item"], em["value"]))
        except Exception as exc:
            logger.warning("东财个股资料失败 %s: %s", code, exc)
            em_map = {}

        def _pick(name: str):
            value = em_map.get(name)
            return None if value in (None, "", "-") else value

        info["name"] = _pick("股票简称")
        info["industry"] = _pick("行业")
        info["list_date"] = pd.to_datetime(_pick("上市时间"), errors="coerce")
        info["total_share"] = pd.to_numeric(_pick("总股本"), errors="coerce")
        info["float_share"] = pd.to_numeric(_pick("流通股"), errors="coerce")
        info["total_mv"] = pd.to_numeric(_pick("总市值"), errors="coerce")
        info["float_mv"] = pd.to_numeric(_pick("流通市值"), errors="coerce")

        try:
            xq = ak.stock_individual_basic_info_xq(symbol=self._xq_symbol(code))
            if xq is not None and not xq.empty:
                xq_map = dict(zip(xq["item"], xq["value"]))
                info["fullname"] = info.get("fullname") or xq_map.get("org_name_cn")
                info["name"] = info.get("name") or xq_map.get("org_short_name_cn")
                info["main_business"] = xq_map.get("main_operation_business")
                info["industry"] = info.get("industry") or xq_map.get("affiliate_industry")
        except Exception as exc:
            logger.warning("雪球个股资料失败 %s: %s", code, exc)

        from data_interface.common.security_code import board_of, exchange_of

        info["exchange"] = exchange_of(code)
        info["board"] = board_of(code)
        info["is_st"] = "ST" in str(info.get("name") or "").upper()
        if info.get("name") is None and not em_map:
            return pd.DataFrame()
        return pd.DataFrame([info])

    @staticmethod
    def _xq_symbol(code: str) -> str:
        if code.startswith("6"):
            return f"SH{code}"
        if code.startswith(("4", "8", "92")):
            return f"BJ{code}"
        return f"SZ{code}"

    def fetch_fund_basic(self, code: str) -> pd.DataFrame:
        ak = self._ak()
        code = normalize_code(code)
        values: dict = {}
        try:
            df = ak.fund_individual_basic_info_xq(symbol=code)
            if df is not None and not df.empty:
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

        ticker_type = self.get_ticker_type(code)
        if not values:
            spot_func = ak.fund_etf_spot_em if ticker_type == "etf" else ak.fund_lof_spot_em
            try:
                spot = spot_func()
                if spot is not None and not spot.empty and "代码" in spot.columns:
                    match = spot[spot["代码"].astype(str) == code]
                    if not match.empty:
                        row = match.iloc[0]
                        values.update(
                            {
                                "name": row.get("名称"),
                                "fullname": row.get("名称"),
                                "fund_type": "ETF" if ticker_type == "etf" else "LOF",
                            }
                        )
            except Exception as exc:
                logger.warning("东财基金列表失败 %s: %s", code, exc)

        if not values:
            return pd.DataFrame()
        if not values.get("fund_type"):
            values["fund_type"] = "ETF" if ticker_type == "etf" else "LOF"
        return pd.DataFrame(
            [
                {
                    "ts": now_cn(),
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
        except Exception as exc:
            logger.warning("概念板块列表失败: %s", exc)
            return pd.DataFrame()
        rename = {}
        for source_col in ("概念名称", "板块名称", "name"):
            if source_col in df.columns:
                rename[source_col] = "name"
                break
        for source_col in ("概念代码", "板块代码", "code"):
            if source_col in df.columns:
                rename[source_col] = "code"
                break
        df = df.rename(columns=rename)
        if "code" not in df.columns or "name" not in df.columns:
            logger.warning("概念板块列名无法识别: %s", list(df.columns)[:8])
            return pd.DataFrame()
        df["ts"] = pd.Timestamp(now_cn().date())
        return df.set_index("ts")

    def fetch_concept_board_ohlc(
        self, board_name: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        ak = self._ak()
        try:
            raw = ak.stock_board_concept_index_ths(
                symbol=board_name, start_date=start_date, end_date=end_date
            )
        except Exception as exc:
            logger.warning("概念板块行情失败 %s: %s", board_name, exc)
            return pd.DataFrame()
        if raw is None or raw.empty:
            return pd.DataFrame()
        frame = _rename_columns(raw.copy())
        if not _require(frame, ("ts", "open", "close")):
            logger.warning("概念板块行情列名无法识别 %s: %s", board_name, list(raw.columns)[:8])
            return pd.DataFrame()
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column not in frame.columns:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["name"] = board_name
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
        return frame.set_index("ts").sort_index()[
            ["name", "open", "high", "low", "close", "volume", "amount"]
        ]

    def fetch_concept_constituents(self, board_name: str) -> pd.DataFrame:
        ak = self._ak()
        try:
            df = ak.stock_board_concept_cons_em(symbol=board_name)
        except Exception as exc:
            logger.warning("概念板块成分失败 %s: %s", board_name, exc)
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
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
        df["stock_code"] = df["stock_code"].astype(str).map(normalize_code)
        df["ts"] = pd.Timestamp(now_cn().date())
        df["board_name"] = board_name
        return df[["ts", "board_name", "stock_code", "stock_name"]]
