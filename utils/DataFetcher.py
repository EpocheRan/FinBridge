"""
从数据库和第三方接口获取数据(前复权)，并转换成dataframe
"""

import pandas as pd
from datetime import datetime, timedelta
import time
import logging
import akshare as ak

# utils
from .DataConnect import questDB, BS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Data Fetcher")


class DataFetcher:
    def __init__(self, source="questdb"):
        """
        初始化数据获取器
        :param source: 数据源 ('akshare', 'baostock', 'questdb')
        """
        self.source = source
        self.db = questDB()  # 默认建立数据库链接，用于支持多源交互
        self.initialize_tables()

    def initialize_tables(self, force=False):
        """
        确保数据库表存在，如果关键表不存在则执行初始化
        :param force: 强制检查所有表格并创建缺失表格
        """
        if not force:
            self._create_tables()
            return

        table_name = self.db.get_ohlc_table_name("d")
        try:
            query = "SELECT * FROM tables();"
            df = pd.read_sql(query, self.db.pd_conn)
            target_col = (
                "name"
                if "name" in df.columns
                else "table_name"
                if "table_name" in df.columns
                else None
            )
            if target_col:
                if table_name not in df[target_col].values:
                    self._create_tables()
            else:
                self._create_tables()
        except Exception:
            self._create_tables()

    def _create_tables(self):
        self._create_ohlc_table()
        self._create_stock_basic_table()
        self._create_fund_basic_table()
        self._create_financial_indicators_table()
        self._create_stock_industry_table()
        self._create_rolling_financial_table()
        self._create_concept_boards_table()
        self._create_concept_ohlc_table()
        self._create_concept_constituents_table()

    def get_ohlc(self, stock_code, start_date, end_date, freq="d") -> pd.DataFrame:
        if self.source == "akshare":
            return self._fetch_from_akshare(stock_code, start_date, end_date, freq)
        elif self.source == "baostock":
            self.bs = BS()
            return self._fetch_from_baostock(stock_code, start_date, end_date, freq)
        elif self.source == "questdb":
            return self._fetch_from_questdb(stock_code, start_date, end_date, freq)
        else:
            raise ValueError(f"未知数据源: {self.source}")

    def get_realtime_quotes(self, ticker=None, source_type=None) -> dict | pd.DataFrame:
        dfs = []
        sources = {
            "stock": ak.stock_zh_a_spot_em,
            "etf": ak.fund_etf_spot_ths,
            "lof": ak.fund_lof_spot_em,
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
        max_retry, retry_delay = 3, 2
        ticker_type = self.get_ticker_type(stock_code)

        if freq.endswith("min"):
            period = int(freq.replace("min", ""))
            all_data = []
            curr, end_dt = (
                datetime.strptime(start_date, "%Y-%m-%d"),
                datetime.strptime(end_date, "%Y-%m-%d"),
            )

            if ticker_type == "etf":
                func = ak.fund_etf_hist_min_em
            elif ticker_type == "lof":
                func = ak.fund_lof_hist_min_em
            else:
                func = ak.stock_zh_a_hist_min_em

            while curr <= end_dt:
                ce = min(curr + timedelta(days=7), end_dt)
                for _ in range(max_retry):
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
                        break
                    except:
                        time.sleep(retry_delay)
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

            if ticker_type == "etf":
                func = ak.fund_etf_hist_em
            elif ticker_type == "lof":
                func = ak.fund_lof_hist_em
            else:
                func = ak.stock_zh_a_hist

            for _ in range(max_retry):
                try:
                    df = func(
                        symbol=stock_code,
                        period=pm[freq],
                        start_date=start_c,
                        end_date=end_c,
                        adjust="qfq",
                    )
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
                except:
                    time.sleep(retry_delay)
        return pd.DataFrame()

    def _fetch_from_baostock(
        self, stock_code, start_date, end_date, freq
    ) -> pd.DataFrame:
        exchange = "sh" if stock_code.startswith("60") else "sz"
        if stock_code.startswith("0000"):
            exchange = "sh"
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
                return df[
                    ["code", "open", "high", "low", "close", "volume", "amount"]
                ].apply(pd.to_numeric, errors="ignore")
            else:
                df["ts"] = pd.to_datetime(df["date"])
                df["code"] = stock_code
                df.rename(columns={"turn": "turnover"}, inplace=True)
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
                ].apply(pd.to_numeric, errors="ignore")
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
                df_xq = ak.stock_individual_basic_info_xq(symbol=code)

                main_biz = None
                affiliate_ind = None
                short_name = None
                fullname = None

                if not df_xq.empty:
                    if "main_operation_biz" in df_xq.columns:
                        main_biz = df_xq["main_operation_biz"].iloc[0]
                    if "affiliate_industry" in df_xq.columns:
                        affiliate_ind = df_xq["affiliate_industry"].iloc[0]
                    if "name" in df_xq.columns:
                        short_name = df_xq["name"].iloc[0]
                    if "org_name" in df_xq.columns:
                        fullname = df_xq["org_name"].iloc[0]

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
                df = ak.fund_individual_basic_info_xq(symbol=code)
                val = {}
                if not df.empty:
                    # Map based on expected columns or common XQ keys
                    row = df.iloc[0]
                    val["name"] = row.get("name")
                    val["fullname"] = row.get("full_name") or row.get("org_name")
                    val["found_date"] = row.get("found_date")
                    val["total_asset"] = row.get("total_asset")
                    val["management"] = row.get("management") or row.get("manager")
                    val["fund_type"] = row.get("type")
                    val["rating_agency"] = row.get("rating_agency")
                    val["rating"] = row.get("rating")

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
                    float(row["turnover"]),
                    str(row["code"]),
                )
                for ts, row in data.iterrows()
            ]
        self.db.batch_insert(q, r)

    def update_stock_basic(self, data: pd.DataFrame):
        q = "INSERT INTO stock_basic_info (ts, code, name, fullname, main_operation_biz, affiliate_industry, total_share, float_share, total_mv, float_mv, list_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        r = [
            (
                ts.strftime("%Y-%m-%d %H:%M:%S"),
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
                ts.strftime("%Y-%m-%d %H:%M:%S"),
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

    def _create_ohlc_table(self):
        freqs = ["1min", "5min", "15min", "30min", "60min", "d", "w", "m"]
        pm = {
            "1min": "HOUR",
            "5min": "HOUR",
            "15min": "DAY",
            "30min": "DAY",
            "60min": "DAY",
            "d": "MONTH",
            "w": "MONTH",
            "m": "YEAR",
        }
        for f in freqs:
            tn, cols = (
                questDB.get_ohlc_table_name(f),
                "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume LONG, amount LONG, code SYMBOL, market SYMBOL",
            )
            if not f.endswith("min"):
                cols = "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume LONG, amount LONG, turnover DOUBLE, code SYMBOL, market SYMBOL"
            self.db.create_table(
                f'CREATE TABLE IF NOT EXISTS "{tn}" ({cols}) TIMESTAMP(ts) PARTITION BY {pm[f]} WAL DEDUP UPSERT KEYS(ts, code, market)'
            )

    def _create_stock_basic_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "stock_basic_info" (ts TIMESTAMP, code SYMBOL, name STRING, fullname STRING, main_operation_biz STRING, affiliate_industry STRING, total_share DOUBLE, float_share DOUBLE, total_mv DOUBLE, float_mv DOUBLE, list_date TIMESTAMP) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
        )

    def _create_fund_basic_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "fund_basic_info" (ts TIMESTAMP, code SYMBOL, name STRING, fullname STRING, found_date TIMESTAMP, total_asset STRING, management STRING, fund_type SYMBOL, rating_agency STRING, rating STRING) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
        )

    def _create_financial_indicators_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "financial_indicators" (ts TIMESTAMP, code SYMBOL, statDate STRING, pubDate STRING, roeAvg DOUBLE, npMargin DOUBLE, gpMargin DOUBLE, netProfit DOUBLE, epsTTM DOUBLE, MBRevenue DOUBLE, totalShare DOUBLE, liqaShare DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
        )

    def _create_stock_industry_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "stock_industry" (ts TIMESTAMP, code SYMBOL, industry_name STRING, industry_classification STRING) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
        )

    def _create_rolling_financial_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "rolling_financial" (ts TIMESTAMP, code SYMBOL, peTTM DOUBLE, psTTM DOUBLE, pcfNcfTTM DOUBLE, pbMRQ DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(code, ts)'
        )

    def _create_concept_constituents_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "concept_constituents" (ts TIMESTAMP, board_name STRING, board_code SYMBOL, stock_name STRING, stock_code SYMBOL) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, board_code, stock_code)'
        )

    def _create_concept_boards_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "concept_boards" (ts TIMESTAMP, code SYMBOL, name STRING) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code)'
        )

    def _create_concept_ohlc_table(self):
        self.db.create_table(
            'CREATE TABLE IF NOT EXISTS "concept_boards_ohlc" (ts TIMESTAMP, code SYMBOL, name STRING, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE) TIMESTAMP(ts) PARTITION BY MONTH WAL DEDUP UPSERT KEYS(ts, code)'
        )

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
