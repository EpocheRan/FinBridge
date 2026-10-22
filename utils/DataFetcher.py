"""
从数据库和第三方接口获取数据(前复权)，并转换成dataframe
"""

import pandas as pd
from datetime import datetime, timedelta
import logging
import akshare as ak

# utils
from .DataConnect import questDB, BS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Data Fetcher")


def _normalize_code(code: str) -> str:
    """去除股票代码后缀，如 600519.SH -> 600519"""
    if isinstance(code, str):
        return code.split(".")[0]
    return str(code)


def _to_baostock_code(code: str) -> str:
    """将 6 位证券代码转换为 baostock 代码 (如 600519 -> sh.600519)"""
    code = _normalize_code(code)
    if code.startswith(("60", "68", "51", "58", "50")):
        return f"sh.{code}"
    elif code.startswith(("4", "8")):
        return f"bj.{code}"
    else:  # 00/30/15/16/18/39 等深市，含 000001 平安银行
        return f"sz.{code}"


class DataFetcher:
    def __init__(self, source="questdb"):
        """
        初始化数据获取器
        :param source: 数据源 ('akshare', 'baostock', 'questdb')
        """
        self.source = source
        self.db = questDB()  # 默认建立数据库链接，用于支持多源交互

    def get_ohlc(self, stock_code, start_date, end_date, freq="d") -> pd.DataFrame:
        # 标准化股票代码
        stock_code = _normalize_code(stock_code)

        if self.source == "akshare":
            return self._fetch_from_akshare(stock_code, start_date, end_date, freq)
        elif self.source == "baostock":
            self.bs = BS()
            return self._fetch_from_baostock(stock_code, start_date, end_date, freq)
        elif self.source == "questdb":
            return self._fetch_from_questdb(stock_code, start_date, end_date, freq)
        else:
            raise ValueError(f"未知数据源: {self.source}")

    def _fetch_tencent_realtime(self, code: str) -> pd.DataFrame:
        """腾讯单只股票实时行情 (qt.gtimg.cn)，避免拉全市场快照，返回 OHLCV DataFrame"""
        import requests

        prefix = (
            "sh"
            if code.startswith(("6", "9", "5"))
            else ("bj" if code.startswith(("4", "8")) else "sz")
        )
        try:
            r = requests.get(f"http://qt.gtimg.cn/q={prefix}{code}", timeout=8)
            r.encoding = "gbk"
            text = r.text
            if '="' not in text:
                return pd.DataFrame()
            data = text.split('="')[1].rstrip('";\n').split("~")
            if len(data) < 40:
                return pd.DataFrame()
            close = float(data[3]) if data[3] else None
            if close is None:
                return pd.DataFrame()
            df = pd.DataFrame(
                [
                    {
                        "code": code,
                        "open": float(data[5]) if data[5] else close,
                        "high": float(data[33]) if data[33] else close,
                        "low": float(data[34]) if data[34] else close,
                        "close": close,
                        "volume": float(data[6]) * 100 if data[6] else 0.0,
                        "amount": float(data[37]) * 10000 if data[37] else 0.0,
                    }
                ]
            )
            df["ts"] = pd.Timestamp.now()
            df.set_index("ts", inplace=True)
            return df[["code", "open", "high", "low", "close", "volume", "amount"]]
        except Exception as e:
            logger.warning(f"腾讯实时行情获取失败 {code}: {e}")
            return pd.DataFrame()

    def get_realtime_quotes(self, ticker=None, source_type=None) -> dict | pd.DataFrame:
        # 单只股票实时行情走腾讯 (快)；未指定 ticker 时才回退到全市场快照
        if ticker:
            return self._fetch_tencent_realtime(_normalize_code(ticker))

        dfs = []
        sources = {
            "stock": ak.stock_zh_a_spot_em,
            "etf": ak.fund_etf_spot_ths,
            "lof": ak.stock_zh_a_spot,
        }
        if source_type in sources:
            try:
                df = sources[source_type]()
                if not df.empty:
                    dfs.append(df)
            except:
                pass
        else:
            for func in sources.values():
                try:
                    df = func()
                    if not df.empty:
                        dfs.append(df)
                except:
                    pass
        if not dfs:
            return {} if ticker is None else pd.DataFrame()
        combined_df = pd.concat(dfs)
        temp_df = pd.DataFrame()
        temp_df["code"] = combined_df["代码"] if "代码" in combined_df.columns else []
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
        for ak_col, target_col in mapping.items():
            if ak_col in combined_df.columns:
                temp_df[target_col] = combined_df[ak_col]
        temp_df["ts"] = pd.Timestamp.now()
        if ticker:
            res = temp_df[temp_df["code"] == ticker].copy()
            if res.empty:
                return pd.DataFrame()
            res.set_index("ts", inplace=True)
            return res[["code", "open", "high", "low", "close", "volume", "amount"]]
        else:
            result_dict = {}
            for code, group in temp_df.groupby("code"):
                df_code = group.copy()
                df_code.set_index("ts", inplace=True)
                result_dict[code] = df_code[
                    ["code", "open", "high", "low", "close", "volume", "amount"]
                ]
            return result_dict

    def get_ticker_type(self, ticker) -> str:
        if ticker.startswith(("51", "15", "50")):
            return "etf"
        elif ticker.startswith(("16")):
            return "lof"
        return "stock"

    def _fetch_from_akshare(
        self, stock_code, start_date, end_date, freq
    ) -> pd.DataFrame:
        ticker_type = self.get_ticker_type(stock_code)

        if freq.endswith("min"):
            period = int(freq.replace("min", ""))
            all_data = []
            curr, end_dt = (
                datetime.strptime(start_date, "%Y-%m-%d"),
                datetime.strptime(end_date, "%Y-%m-%d"),
            )

            if ticker_type in ["etf", "lof"]:
                # Use Sina for fund min data
                prefix = (
                    "sz"
                    if stock_code.startswith(("15", "16", "18", "39", "159"))
                    else "sh"
                )
                try:
                    df = ak.stock_zh_a_minute(
                        symbol=f"{prefix}{stock_code}", period=f"{period}", adjust="qfq"
                    )
                    if not df.empty:
                        df.columns = ["ts", "open", "high", "low", "close", "volume"]
                        df["amount"] = 0  # Sina minute doesn't provide amount
                        df["ts"] = pd.to_datetime(df["ts"])
                        df["code"] = stock_code
                        # Filter by date range
                        start_dt = pd.to_datetime(start_date)
                        end_dt = pd.to_datetime(end_date) + timedelta(days=1)
                        df = df[(df["ts"] >= start_dt) & (df["ts"] < end_dt)]
                        df.set_index("ts", inplace=True)
                        return df[
                            ["code", "open", "high", "low", "close", "volume", "amount"]
                        ]
                except Exception as e:
                    logger.warning(f"Sina min fetch failed for {stock_code}: {e}")
                    return pd.DataFrame()
            else:
                func = ak.stock_zh_a_hist_min_em
                while curr <= end_dt:
                    ce = min(curr + timedelta(days=7), end_dt)
                    try:
                        df = func(
                            symbol=stock_code,
                            period=f"{period}",
                            start_date=curr.strftime("%Y%m%d"),
                            end_date=ce.strftime("%Y%m%d"),
                            adjust="qfq",
                        )
                        if not df.empty:
                            all_data.append(df)
                    except:
                        pass
                    curr = ce + timedelta(days=1)
                if not all_data:
                    return pd.DataFrame()
                df = pd.concat(all_data)
                df.columns = (
                    [
                        "ts",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "amount",
                        "dummy1",
                        "dummy2",
                    ]
                    if len(df.columns) == 9
                    else ["ts", "open", "high", "low", "close", "volume", "amount"]
                )
                df["ts"] = pd.to_datetime(df["ts"])
                df["code"] = stock_code
                df.set_index("ts", inplace=True)
                return df[["code", "open", "high", "low", "close", "volume", "amount"]]
        else:
            pm = {"d": "daily", "w": "weekly", "m": "monthly"}
            start_c, end_c = start_date.replace("-", ""), end_date.replace("-", "")

            if ticker_type in ["etf", "lof"]:
                try:
                    prefix = (
                        "sz"
                        if stock_code.startswith(("15", "16", "18", "39", "159"))
                        else "sh"
                    )
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
                except Exception as e:
                    logger.warning(f"Sina fetch failed for {stock_code}: {e}")
                    return pd.DataFrame()
            else:
                # Use Sina for stock daily data as Eastmoney is often blocked
                prefix = "sh" if stock_code.startswith("6") else "sz"
                if stock_code.startswith(("4", "8")):
                    prefix = "bj"

                try:
                    df = ak.stock_zh_a_daily(
                        symbol=f"{prefix}{stock_code}",
                        start_date=start_c,
                        end_date=end_c,
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
                except Exception as e:
                    logger.warning(
                        f"Sina stock_zh_a_daily failed for {stock_code}: {e}"
                    )
                    return pd.DataFrame()

            if df.empty:
                return pd.DataFrame()

            df = df[
                [
                    "日期",
                    "开盘",
                    "最高",
                    "最低",
                    "收盘",
                    "成交量",
                    "成交额",
                    "换手率",
                ]
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
                [
                    "code",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "turnover",
                ]
            ]

    def _fetch_from_baostock(
        self, stock_code, start_date, end_date, freq
    ) -> pd.DataFrame:
        # Improved exchange identification
        if stock_code.startswith(("60", "68", "51", "58", "50", "000001")):
            # Note: 000001 can be SZ stock or SH Index.
            # In most cases for stocks we mean SZ.
            # But Baostock uses sh.000001 for SSE Index.
            # If it's exactly 000001 and we are in a stock context, it's tricky.
            # However, for now let's follow a more comprehensive rule.
            if stock_code == "000001":
                # Check if it's likely the index or stock.
                # This is a heuristic. Default to sz for 002977 etc.
                exchange = "sz"
            elif stock_code.startswith(("60", "68", "51", "58", "50")):
                exchange = "sh"
            else:
                exchange = "sz"
        elif stock_code.startswith(("00", "30", "15", "16", "18")):
            exchange = "sz"
        elif stock_code.startswith(("4", "8")):
            exchange = "bj"
        else:
            exchange = "sz"

        # Special case for SSE Index if needed, but usually codes are handled via ticker_type
        if stock_code == "000001" and not self.get_ticker_type(stock_code) == "stock":
            # This is still not perfect as get_ticker_type returns 'stock' for 000001
            pass

        full_code = f"{exchange}.{stock_code}"
        fm = {
            "5min": "5",
            "15min": "15",
            "30min": "30",
            "60min": "60",
            "d": "d",
            "w": "w",
            "m": "m",
        }
        try:
            if freq.endswith("min"):
                rs = self.bs().query_history_k_data_plus(
                    full_code,
                    "date,time,open,high,low,close,volume,amount",
                    start_date=start_date,
                    end_date=end_date,
                    frequency=fm[freq],
                    adjustflag="2",
                )
            else:
                rs = self.bs().query_history_k_data_plus(
                    full_code,
                    "date,open,high,low,close,volume,amount,turn",
                    start_date=start_date,
                    end_date=end_date,
                    frequency=fm[freq],
                    adjustflag="2",
                )
            data = []
            while rs.next():
                data.append(rs.get_row_data())
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=rs.fields)
            if freq.endswith("min"):
                df["ts"] = pd.to_datetime(df["time"].str[:12])
                df["code"] = stock_code
                df.set_index("ts", inplace=True)
                out = df[
                    ["code", "open", "high", "low", "close", "volume", "amount"]
                ].copy()
                num_cols = ["open", "high", "low", "close", "volume", "amount"]
                out[num_cols] = out[num_cols].apply(pd.to_numeric, errors="coerce")
                return out
            else:
                df["ts"] = pd.to_datetime(df["date"])
                df["code"] = stock_code
                df.rename(columns={"turn": "turnover"}, inplace=True)
                df.set_index("ts", inplace=True)
                out = df[
                    [
                        "code",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "amount",
                        "turnover",
                    ]
                ].copy()
                num_cols = [
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "turnover",
                ]
                out[num_cols] = out[num_cols].apply(pd.to_numeric, errors="coerce")
                return out
        except:
            return pd.DataFrame()

    def _fetch_from_questdb(
        self, stock_code, start_date, end_date, freq
    ) -> pd.DataFrame:
        tn = questDB.get_ohlc_table_name(freq)
        f = (
            "ts, code, open, high, low, close, volume, amount, turnover"
            if freq in ["d", "w", "m"]
            else "ts, code, open, high, low, close, volume, amount"
        )
        return self._read_sql_to_df(
            f"SELECT {f} FROM \"{tn}\" WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{stock_code}' ORDER BY ts"
        )

    def get_stock_basic_info(self, code=None) -> pd.DataFrame:
        if code:
            code = _normalize_code(code)

        if self.source == "questdb":
            q = (
                f"SELECT * FROM \"stock_basic_info\" WHERE code = '{code}' LATEST ON ts PARTITION BY code"
                if code
                else 'SELECT * FROM "stock_basic_info" LATEST ON ts PARTITION BY code'
            )
            return self._read_sql_to_df(q)
        elif self.source == "akshare":
            if not code:
                return pd.DataFrame()
            try:
                # Get data from EM
                df_em = ak.stock_individual_info_em(symbol=code)
                em_map = dict(zip(df_em["item"], df_em["value"]))

                # Get data from XQ
                xq_code = code
                if code.startswith("6"):
                    xq_code = f"SH{code}"
                elif code.startswith(("0", "3")):
                    xq_code = f"SZ{code}"
                elif code.startswith(("4", "8")):
                    xq_code = f"BJ{code}"

                df_xq = ak.stock_individual_basic_info_xq(symbol=xq_code)

                main_biz = None
                affiliate_ind = None
                short_name = None
                fullname = None

                if (
                    not df_xq.empty
                    and "item" in df_xq.columns
                    and "value" in df_xq.columns
                ):
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
                    "list_date": pd.to_datetime(
                        em_map.get("上市时间"), errors="coerce"
                    ),
                }
                return pd.DataFrame([data])
            except Exception as e:
                logger.error(f"Error fetching basic info from akshare for {code}: {e}")
                return pd.DataFrame()
        elif self.source == "baostock":
            if not code:
                return pd.DataFrame()
            try:
                bs = BS()
                full_code = _to_baostock_code(code)

                # 1. 名称 + 上市日期 (baostock)
                name, list_date = None, None
                rs = bs().query_stock_basic(code=full_code)
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    row = dict(zip(rs.fields, rows[0]))
                    name = row.get("code_name")
                    list_date = pd.to_datetime(row.get("ipoDate"), errors="coerce")
                else:
                    return pd.DataFrame()

                # 2. 行业 (baostock)
                affiliate_industry = None
                try:
                    rs = bs().query_stock_industry(code=full_code)
                    inds = []
                    while rs.error_code == "0" and rs.next():
                        inds.append(rs.get_row_data())
                    if inds:
                        affiliate_industry = dict(zip(rs.fields, inds[0])).get("industry")
                except Exception as e:
                    logger.warning(f"baostock 行业获取失败 {code}: {e}")

                # 3. 总股本/流通股本 (baostock 财报, 取最近一期)
                total_share, float_share = None, None
                try:
                    y, q = datetime.now().year, (datetime.now().month - 1) // 3 + 1
                    for _ in range(4):
                        rs = bs().query_profit_data(code=full_code, year=y, quarter=q)
                        fin = []
                        while rs.error_code == "0" and rs.next():
                            fin.append(rs.get_row_data())
                        if fin:
                            frow = dict(zip(rs.fields, fin[0]))
                            total_share = pd.to_numeric(frow.get("totalShare"), errors="coerce")
                            float_share = pd.to_numeric(frow.get("liqaShare"), errors="coerce")
                            break
                        q -= 1
                        if q == 0:
                            y, q = y - 1, 4
                except Exception as e:
                    logger.warning(f"baostock 股本获取失败 {code}: {e}")

                # 4. 全称 + 主营业务 (巨潮资讯 cninfo)
                fullname, main_operation_biz = None, None
                try:
                    prof = ak.stock_profile_cninfo(symbol=code)
                    if not prof.empty:
                        fullname = prof["公司名称"].iloc[0] if "公司名称" in prof.columns else None
                        main_operation_biz = prof["主营业务"].iloc[0] if "主营业务" in prof.columns else None
                except Exception as e:
                    logger.warning(f"cninfo 全称/主营业务获取失败 {code}: {e}")

                # 5. 总市值 (百度估值, 单位亿元 -> 元), 并推算流通市值
                total_mv, float_mv = None, None
                try:
                    mv = ak.stock_zh_valuation_baidu(symbol=code, indicator="总市值", period="近一年")
                    if not mv.empty and pd.notna(mv["value"].iloc[-1]):
                        total_mv = float(mv["value"].iloc[-1]) * 1e8
                        if pd.notna(total_share) and pd.notna(float_share) and total_share > 0:
                            float_mv = total_mv * float(float_share) / float(total_share)
                except Exception as e:
                    logger.warning(f"百度总市值获取失败 {code}: {e}")

                data = {
                    "ts": pd.Timestamp.now(),
                    "code": code,
                    "name": name,
                    "fullname": fullname,
                    "main_operation_biz": main_operation_biz,
                    "affiliate_industry": affiliate_industry,
                    "total_share": total_share,
                    "float_share": float_share,
                    "total_mv": total_mv,
                    "float_mv": float_mv,
                    "list_date": list_date,
                }
                return pd.DataFrame([data])
            except Exception as e:
                logger.error(f"Error fetching basic info from baostock for {code}: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    def get_fund_basic_info(self, code=None) -> pd.DataFrame:
        if self.source == "questdb":
            q = (
                f"SELECT * FROM \"fund_basic_info\" WHERE code = '{code}' LATEST ON ts PARTITION BY code"
                if code
                else 'SELECT * FROM "fund_basic_info" LATEST ON ts PARTITION BY code'
            )
            return self._read_sql_to_df(q)
        elif self.source == "akshare":
            if not code:
                return pd.DataFrame()
            try:
                # 1. 尝试从雪球获取
                val = {}
                try:
                    df = ak.fund_individual_basic_info_xq(symbol=code)
                    if not df.empty and "item" in df.columns and "value" in df.columns:
                        xq_map = dict(zip(df["item"], df["value"]))
                        val["name"] = xq_map.get("基金名称")
                        val["fullname"] = xq_map.get("基金全称")
                        val["found_date"] = xq_map.get("成立时间")
                        val["total_asset"] = xq_map.get("最新规模")
                        val["management"] = xq_map.get("基金公司")
                        val["fund_type"] = xq_map.get("基金类型")
                        val["rating_agency"] = xq_map.get("评级机构")
                        val["rating"] = xq_map.get("基金评级")
                except Exception as e:
                    logger.warning(f"Snowball (XQ) fetch failed for {code}: {e}")

                # 2. 如果获取不到，尝试备选方案
                if not val:
                    # 针对深市 ETF (15, 16, 18, 39 开头)
                    if code.startswith(("15", "16", "18", "39")):
                        try:
                            df_szse = ak.fund_etf_scale_szse()
                            if not df_szse.empty:
                                match = df_szse[df_szse["基金代码"].astype(str) == code]
                                if not match.empty:
                                    row = match.iloc[0]
                                    val["name"] = row.get("基金简称")
                                    val["fullname"] = row.get("基金简称")
                                    val["total_asset"] = str(row.get("基金份额"))
                                    val["fund_type"] = "ETF"
                        except Exception as e_szse:
                            logger.warning(
                                f"Error fetching from szse fallback for {code}: {e_szse}"
                            )

                    # 针对沪市 ETF (51, 58, 68, 50 开头)
                    elif code.startswith(("51", "58", "68", "50")):
                        try:
                            df_spot = ak.fund_etf_scale_sse()
                            if not df_spot.empty:
                                match = df_spot[df_spot["基金代码"] == code]
                                if not match.empty:
                                    row = match.iloc[0]
                                    val["name"] = row.get("基金简称")
                                    val["fullname"] = row.get("基金简称")
                                    val["fund_type"] = "ETF"
                        except Exception as e:
                            logger.warning(f"SH ETF fallback failed for {code}: {e}")

                if not val:
                    return pd.DataFrame()

                data = {
                    "ts": pd.Timestamp.now(),
                    "code": code,
                    "name": val.get("name"),
                    "fullname": val.get("fullname"),
                    "found_date": pd.to_datetime(
                        val.get("found_date"), errors="coerce"
                    ),
                    "total_asset": str(val.get("total_asset"))
                    if val.get("total_asset")
                    else None,
                    "management": val.get("management"),
                    "fund_type": val.get("fund_type"),
                    "rating_agency": val.get("rating_agency"),
                    "rating": str(val.get("rating")) if val.get("rating") else None,
                }
                return pd.DataFrame([data])
            except Exception as e:
                logger.error(
                    f"Error fetching fund basic info from akshare for {code}: {e}"
                )
                return pd.DataFrame()
        return pd.DataFrame()

    def get_financial_indicators(self, stock_code, year, quarter) -> pd.DataFrame:
        if self.source == "questdb":
            qe = {1: "-03-31", 2: "-06-30", 3: "-09-30", 4: "-12-31"}[quarter]
            return self._read_sql_to_df(
                f"SELECT * FROM \"financial_table\" WHERE ts = '{year}{qe}'::timestamp AND code = '{stock_code}'"
            )
        return pd.DataFrame()

    def get_stock_industry(self, code=None) -> pd.DataFrame:
        if self.source == "questdb":
            q = (
                f"SELECT * FROM \"stock_industry\" WHERE code = '{code}' LATEST ON ts"
                if code
                else 'SELECT * FROM "stock_industry" LATEST ON ts PARTITION BY code'
            )
            return self._read_sql_to_df(q)
        return pd.DataFrame()

    def get_rolling_financial(self, stock_code, start_date, end_date) -> pd.DataFrame:
        if self.source == "questdb":
            tn = "rolling_financial"
            return self._read_sql_to_df(
                f"SELECT * FROM \"{tn}\" WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{stock_code}' ORDER BY ts"
            )
        elif self.source == "baostock":
            try:
                bs = BS()
                rs = bs().query_history_k_data_plus(
                    _to_baostock_code(stock_code),
                    "date,peTTM,psTTM,pcfNcfTTM,pbMRQ",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="3",
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return pd.DataFrame()
                df = pd.DataFrame(rows, columns=rs.fields)
                df["ts"] = pd.to_datetime(df["date"])
                df["code"] = stock_code
                for col in ["peTTM", "psTTM", "pcfNcfTTM", "pbMRQ"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df.set_index("ts", inplace=True)
                return df[["code", "peTTM", "psTTM", "pcfNcfTTM", "pbMRQ"]]
            except Exception as e:
                logger.error(
                    f"Error fetching rolling financial from baostock for {stock_code}: {e}"
                )
                return pd.DataFrame()
        return pd.DataFrame()

    def get_concept_boards(self) -> pd.DataFrame:
        if self.source == "questdb":
            return self._read_sql_to_df(
                'SELECT * FROM "concept_boards" LATEST ON ts PARTITION BY code'
            )
        elif self.source == "akshare":
            try:
                df = ak.stock_board_concept_name_ths()
                df["ts"] = pd.Timestamp.today().date()
                df.set_index("ts", inplace=True)
                return df
            except:
                return pd.DataFrame()
        return pd.DataFrame()

    def get_concept_boards_ohlc(self, board_name, start_date, end_date) -> pd.DataFrame:
        bc = self.get_board_code_by_name(board_name)
        if self.source == "questdb":
            return self._read_sql_to_df(
                f"SELECT * FROM \"concept_boards_ohlc\" WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{bc}' ORDER BY ts"
            )
        elif self.source == "akshare":
            try:
                df = ak.stock_board_concept_index_ths(
                    symbol=board_name, start_date=start_date, end_date=end_date
                )
                df.columns = ["ts", "open", "high", "low", "close", "volume", "amount"]
                df["code"], df["name"] = bc, board_name
                return df
            except:
                return pd.DataFrame()
        return pd.DataFrame()

    def get_concept_constituents(self, board_name) -> pd.DataFrame:
        bc = self.get_board_code_by_name(board_name)
        if self.source == "questdb":
            return self._read_sql_to_df(
                f"SELECT * FROM \"concept_constituents\" WHERE board_code = '{bc}' LATEST ON ts PARTITION BY stock_code"
            )
        elif self.source == "akshare":
            try:
                df = ak.stock_board_concept_cons_em(symbol=board_name)
                df["ts"], df["board_code"], df["board_name"] = (
                    pd.Timestamp.today().date(),
                    bc,
                    board_name,
                )
                return df
            except:
                return pd.DataFrame()
        return pd.DataFrame()

    def get_board_code_by_name(self, board_name) -> str | None:
        try:
            df = pd.read_sql(
                f"SELECT code FROM \"concept_boards\" WHERE name = '{board_name}' LATEST ON ts PARTITION BY code",
                self.db.pd_conn,
            )
            if not df.empty:
                return df["code"].iloc[0]
        except:
            pass
        df = self.get_concept_boards()
        if df.empty:
            return None
        self.update_concept_boards(df)
        m = df[df["name"] == board_name]
        return m["code"].iloc[0] if not m.empty else None

    def update_ohlc(self, data: pd.DataFrame, freq):
        if data.empty:
            return
        tn = questDB.get_ohlc_table_name(freq)
        if freq.endswith("min"):
            q = f"INSERT INTO \"{tn}\" (ts, open, high, low, close, volume, amount, code, market) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'MLCN')"
            r = [
                (
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["volume"]),
                    float(row["amount"]),
                    str(row["code"]),
                )
                for ts, row in data.iterrows()
            ]
        else:
            q = f"INSERT INTO \"{tn}\" (ts, open, high, low, close, volume, amount, turnover, code, market) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'MLCN')"
            r = [
                (
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["volume"]),
                    float(row["amount"]),
                    float(row.get("turnover", 0)),
                    str(row["code"]),
                )
                for ts, row in data.iterrows()
            ]
        self.db.batch_insert(q, r)

    def update_stock_basic(self, data: pd.DataFrame):
        q = "INSERT INTO stock_basic_info (ts, code, name, fullname, main_operation_biz, affiliate_industry, total_share, float_share, total_mv, float_mv, list_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        r = [
            (
                row["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                str(row.get("name")) if pd.notna(row.get("name")) else None,
                str(row.get("fullname")) if pd.notna(row.get("fullname")) else None,
                str(row.get("main_operation_biz"))
                if pd.notna(row.get("main_operation_biz"))
                else None,
                str(row.get("affiliate_industry"))
                if pd.notna(row.get("affiliate_industry"))
                else None,
                float(row.get("total_share"))
                if pd.notna(row.get("total_share"))
                else None,
                float(row.get("float_share"))
                if pd.notna(row.get("float_share"))
                else None,
                float(row.get("total_mv")) if pd.notna(row.get("total_mv")) else None,
                float(row.get("float_mv")) if pd.notna(row.get("float_mv")) else None,
                row.get("list_date") if pd.notna(row.get("list_date")) else None,
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_fund_basic(self, data: pd.DataFrame):
        q = "INSERT INTO fund_basic_info (ts, code, name, fullname, found_date, total_asset, management, fund_type, rating_agency, rating) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        r = [
            (
                row["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                str(row.get("name")) if pd.notna(row.get("name")) else None,
                str(row.get("fullname")) if pd.notna(row.get("fullname")) else None,
                row.get("found_date") if pd.notna(row.get("found_date")) else None,
                str(row.get("total_asset"))
                if pd.notna(row.get("total_asset"))
                else None,
                str(row.get("management")) if pd.notna(row.get("management")) else None,
                str(row.get("fund_type")) if pd.notna(row.get("fund_type")) else None,
                str(row.get("rating_agency"))
                if pd.notna(row.get("rating_agency"))
                else None,
                str(row.get("rating")) if pd.notna(row.get("rating")) else None,
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_financial_indicators(self, data: pd.DataFrame):
        q = "INSERT INTO financial_indicators (ts, code, statDate, pubDate, roeAvg, npMargin, gpMargin, netProfit, epsTTM, MBRevenue, totalShare, liqaShare) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                str(row["statDate"]),
                str(row["pubDate"]),
                float(row["roeAvg"]),
                float(row["npMargin"]),
                float(row["gpMargin"]),
                float(row["netProfit"]),
                float(row["epsTTM"]),
                float(row["MBRevenue"]),
                float(row["totalShare"]),
                float(row["liqaShare"]),
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_stock_industry(self, data: pd.DataFrame):
        q = "INSERT INTO stock_industry (ts, code, industry_name, industry_classification) VALUES (%s, %s, %s, %s)"
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                str(row["industry_name"]),
                str(row["industry_classification"]),
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_rolling_financial(self, data: pd.DataFrame):
        q = "INSERT INTO rolling_financial (ts, code, peTTM, psTTM, pcfNcfTTM, pbMRQ) VALUES (%s, %s, %s, %s, %s, %s)"
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                float(row["peTTM"]),
                float(row["psTTM"]),
                float(row["pcfNcfTTM"]),
                float(row["pbMRQ"]),
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_concept_boards(self, data: pd.DataFrame):
        q = 'INSERT INTO "concept_boards" (ts, code, name) VALUES (%s, %s, %s)'
        r = [
            (ts.strftime("%Y-%m-%d %H:%M:%S"), str(row["code"]), str(row["name"]))
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_concept_boards_ohlc(self, data: pd.DataFrame):
        q = 'INSERT INTO "concept_boards_ohlc" (ts, code, name, open, high, low, close, volume, amount) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)'
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                str(row["code"]),
                str(row["name"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
                float(row["amount"]),
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def update_concept_constituents(self, data: pd.DataFrame):
        q = 'INSERT INTO "concept_constituents" (ts, board_name, board_code, stock_name, stock_code) VALUES (%s, %s, %s, %s, %s)'
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                str(row["board_name"]),
                str(row["board_code"]),
                str(row["stock_name"]),
                str(row["stock_code"]),
            )
            for ts, row in data.iterrows()
        ]
        self.db.batch_insert(q, r)

    def _read_sql_to_df(self, q):
        try:
            df = pd.read_sql(q, self.db.pd_conn)
            if not df.empty:
                df["ts"] = pd.to_datetime(df["ts"])
                df.set_index("ts", inplace=True)
            return df
        except:
            return pd.DataFrame()


if __name__ == "__main__":
    fetcher = DataFetcher(source="akshare")
    print(fetcher.get_realtime_quotes(ticker="600519"))
