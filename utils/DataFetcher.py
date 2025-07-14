"""
从数据库和第三方接口获取数据(前复权)，并转换成dataframe
"""

import pandas as pd
from datetime import datetime, timedelta
import time
import logging
import akshare as ak

# utils
# from .DataConnect import questDB, BS
from DataConnect import questDB, BS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Data Fetcher')

class DataFetcher():
    def __init__(self, source='questdb'):
        '''
        初始化数据获取器
        :param source: 数据源 ('akshare', 'baostock', 'questdb')
        '''
        self.source = source
        self.db = questDB() #默认建立数据库链接，用于支持多源交互
        self.initialize_tables(self)

    def initialize_tables(self, force=False):
        """
        确保数据库表存在，如果关键表不存在则执行初始化
        :param force: 强制检查所有表格并创建缺失表格
        """
        if not force: 
            self._create_tables()
            return

        # 检查关键表（ohlc_d表）是否存在
        table_name = self.db.get_ohlc_table_name('d')
        query = f"SELECT name FROM tables() WHERE name = '{table_name}';"
        
        try:
            # 尝试查询表是否存在
            df = pd.read_sql(query, self.db.pd_conn)
            if df.empty:
                logger.info(f"关键表 {table_name} 不存在，执行初始化...")
                self._create_tables()

        except Exception as e:
            # 如果查询出错（通常是因为表不存在），执行初始化
            logger.warning(f"表检查失败: {e}，执行初始化...")
            self._create_tables()

    def _create_tables(self):
        logger.info("Initializing database tables...")
        self._create_ohlc_table()
        self._create_stock_basic_table()
        self._create_financial_indicators_table()
        self._create_stock_industry_table()
        self._create_rolling_financial_table()
        self._create_concept_boards_table()
        self._create_concept_ohlc_table()
        self._create_concept_constituents_table()
        logger.info("Database tables initialized")

    def get_ohlc(self, stock_code, start_date, end_date, freq='d') -> pd.DataFrame:
        """
        获取股票ohlc数据

        :param stock_code: 股票代码 (如 '603286')
        :param start_date: 开始日期 (格式 'YYYY-MM-DD')
        :param end_date: 结束日期 (格式 'YYYY-MM-DD')
        :param freq: 数据频率 
            ('1min', '5min', '15min', '30min', '60min', 'd', 'w', 'm')
        :return: 包含股票数据的DataFrame
        """
        if self.source == 'akshare':
            return self._fetch_from_akshare(stock_code, start_date, end_date, freq)
        elif self.source == 'baostock':
            self.bs = BS()
            return self._fetch_from_baostock(stock_code, start_date, end_date, freq)
        elif self.source == 'questdb':
            self.db = questDB()
            return self._fetch_from_questdb(stock_code, start_date, end_date, freq)
        else:
            raise ValueError(f"未知数据源: {self.source}")
   
    def _fetch_from_akshare(self, stock_code, start_date, end_date, freq) -> pd.DataFrame:
            """从akshare获取数据"""
            max_retry = 3
            retry_delay = 2
            
            # 分钟级数据
            if freq.endswith('min'):
                period = int(freq.replace('min', ''))
                
                # akshare的分钟数据接口限制较多，需要分多次获取
                all_data = []
                current_start = datetime.strptime(start_date, '%Y-%m-%d')
                end_dt = datetime.strptime(end_date, '%Y-%m-%d')
                
                while current_start <= end_dt:
                    # 每次最多获取7天数据
                    current_end = min(current_start + timedelta(days=7), end_dt)
                    
                    for attempt in range(max_retry):
                        try:
                            if stock_code.startswith('51') or stock_code.startswith('15'):
                                # 如果为ETF则调用etf查询接口，最多五个交易日
                                df = ak.fund_etf_hist_min_em(
                                    symbol=stock_code,
                                    period=f"{period}",
                                    start_date=current_start.strftime('%Y%m%d'),
                                    end_date=current_end.strftime('%Y%m%d'),
                                    adjust='qfq'
                                )
                            else:   
                                df = ak.stock_zh_a_hist_min_em(
                                    symbol=stock_code,
                                    period=f"{period}",
                                    start_date=current_start.strftime('%Y%m%d'),
                                    end_date=current_end.strftime('%Y%m%d'),
                                    adjust='qfq'
                                )
                            if not df.empty:
                                all_data.append(df)
                            break
                        except Exception as e:
                            if attempt < max_retry - 1:
                                logger.error(f"akshare分钟数据获取失败 (尝试 {attempt+1}/{max_retry}), 等待 {retry_delay}秒后重试...")
                                time.sleep(retry_delay)
                            else:
                                raise RuntimeError(f"akshare分钟数据获取失败: {e}")
                    
                    current_start = current_end + timedelta(days=1)
                
                if not all_data:
                    return pd.DataFrame()
                
                df = pd.concat(all_data)
                # 数据清洗
                df = df[['时间', '开盘', '最高', '最低', '收盘', '成交量', '成交额']]
                df.columns = ['ts', 'open', 'high', 'low', 'close', 'volume', 'amount']
                df['ts'] = pd.to_datetime(df['ts'])
                df['code'] = stock_code
                df = df[['ts', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount']]
                df.set_index('ts', inplace=True)
                return df
            
            # 日线及以上数据
            else:
                # 映射akshare的周期参数
                period_map = {
                    'd': 'daily',
                    'w': 'weekly',
                    'm': 'monthly'
                }
                
                if freq not in period_map:
                    raise ValueError(f"akshare不支持该频率: {freq}")
                
                # 转换日期格式
                start_date = start_date.replace('-', '')
                end_date = end_date.replace('-', '')
                
                for attempt in range(max_retry):
                    try:
                        if stock_code.startswith('51') or stock_code.startswith('15'):
                            # 如果为ETF则调用etf查询接口
                            df = ak.fund_etf_hist_em(
                                symbol=stock_code, 
                                period=period_map[freq],
                                start_date=start_date, 
                                end_date=end_date,
                                adjust="qfq"
                            )
                        else:    
                            df = ak.stock_zh_a_hist(
                                symbol=stock_code, 
                                period=period_map[freq],
                                start_date=start_date, 
                                end_date=end_date,
                                adjust="qfq"
                            )
                        
                        # 数据清洗
                        df = df[['日期', '开盘', '最高', '最低', '收盘', '成交量', '成交额', '换手率']]
                        df.columns = ['ts', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover']
                        df['ts'] = pd.to_datetime(df['ts'])
                        df['code'] = stock_code
                        df = df[['ts', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover']]
                        df.set_index('ts', inplace=True)
                        return df
                    except Exception as e:
                        if attempt < max_retry - 1:
                            logger.error(f"akshare数据获取失败 (尝试 {attempt+1}/{max_retry}), 等待 {retry_delay}秒后重试...")
                            time.sleep(retry_delay)
                        else:
                            raise RuntimeError(f"akshare数据获取失败: {e}")
        
    def _fetch_from_baostock(self, stock_code, start_date, end_date, freq) -> pd.DataFrame:
        """从baostock获取数据"""
        # 转换股票代码格式
        exchange = 'sh' if stock_code.startswith('60') else 'sz'
        if stock_code.startswith('0000') : exchange = 'sh'

        full_code = f"{exchange}.{stock_code}"
        
        # 映射baostock的频率参数
        freq_map = {
            '5min': '5',
            '15min': '15',
            '30min': '30',
            '60min': '60',
            'd': 'd',
            'w': 'w',
            'm': 'm'
        }
        
        if freq not in freq_map:
            raise ValueError(f"baostock不支持该频率: {freq}")
        
        try:
            # 查询数据
            if freq.endswith('min'):
                rs = self.bs().query_history_k_data_plus(
                    full_code,
                    "date,time,open,high,low,close,volume,amount",
                    start_date=start_date,
                    end_date=end_date,
                    frequency=freq_map[freq],
                    adjustflag="2"  # 前复权
                )
            else:
                rs = self.bs().query_history_k_data_plus(
                    full_code,
                    "date,open,high,low,close,volume,amount,turn",
                    start_date=start_date,
                    end_date=end_date,
                    frequency=freq_map[freq],
                    adjustflag="2"  # 前复权
                )
            
            # 转换为DataFrame
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(data_list, columns=rs.fields)
            
            # 数据清洗
            if freq.endswith('min'):
                # 分钟数据包含日期和时间
                df['ts'] = pd.to_datetime(df['time'].str[:12])
                df['code'] = stock_code
                df = df[['ts', 'code', 'open', 'high', 'low', 'close', 'volume','amount']]
                df.set_index('ts', inplace=True)
            else:
                # 日线及以上只有日期
                df = df[['date', 'open', 'high', 'low', 'close', 'volume','amount','turn']]
                df.rename(columns={'turn': 'turnover'}, inplace=True)
                df['ts'] = pd.to_datetime(df['date'])
                df['code'] = stock_code
                df = df[['ts', 'code', 'open', 'high', 'low', 'close', 'volume','amount', 'turnover']]
                df.set_index('ts', inplace=True)
            
            # 转换数据类型
            # df = df.apply(pd.to_numeric, errors='ignore')
            df = df.apply(pd.to_numeric)
            return df
        except Exception as e:
            raise RuntimeError(f"baostock数据获取失败: {e}")
    
    def _fetch_from_questdb(self, stock_code, start_date, end_date, freq) -> pd.DataFrame:
        """从QuestDB获取数据"""

        # 根据频率确定表名
        table_name = questDB.get_ohlc_table_name(freq)

        # 构建查询字段 - 所有频率都包含amount，日/周/月额外包含turnover
        if freq in ['d', 'w', 'm']:
            fields = "ts, code, open, high, low, close, volume, amount, turnover"
        else:
            fields = "ts, code, open, high, low, close, volume, amount"

        query = f"""
            SELECT {fields}
            FROM "{table_name}"
            WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{stock_code}'
            ORDER BY ts
        """
        error_str = f"提示: QuestDB表 '{table_name}' 不存在"
        return self._read_sql_to_df(query, error_str)
    
    def get_stock_basic_info(self) -> pd.DataFrame:
        """获取全量股票基本信息"""
        if self.source == 'questdb':
            query = f"""
                SELECT *
                FROM "stock_basic_info"
                LATEST ON ts PARTITION BY code;
            """
            error_str = f"提示: QuestDB表 'stock_basic_info' 不存在"
            return self._read_sql_to_df(query, error_str)
        
        elif self.source == 'baostock':
            # 请求全量基础数据
            self.bs = BS()
            rs = self.bs().query_stock_basic()
            
            # 转换为DataFrame
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(data_list, columns=rs.fields)
            df.rename(columns={'code_name': 'name'}, inplace=True)

            #处理Baostock返回的code前的交易所编码
            df['code'] = df['code'].str.split('.').str[-1] 
            df['ts'] = pd.Timestamp.today().date() 
            df = df[['ts', 'code', 'name', 'ipoDate', 'outDate', 'type','status']]
            df.set_index('ts', inplace=True)
            return df
        else:
            logger.error("仅支持从 questdb 或 baostock 获取全量股票基本信息")

    def get_financial_indicators(self, stock_code, year, quarter) -> pd.DataFrame:
        """获取财务指标数据"""
        if self.source == 'questdb':     
            query = f"""
                SELECT * 
                FROM "financial_table" 
                WHERE 
                    ts = ({year} || 
                        CASE {quarter} 
                            WHEN 1 THEN '-03-31' 
                            WHEN 2 THEN '-06-30' 
                            WHEN 3 THEN '-09-30' 
                            WHEN 4 THEN '-12-31' 
                        END)::timestamp
                    AND code = {stock_code};
            """
            error_str = f"提示: QuestDB表 'financial_table' 不存在"
            return self._read_sql_to_df(query, error_str)
        
        elif self.source == 'baostock':
            self.bs = BS()
            # 转换股票代码格式
            exchange = 'sh' if stock_code.startswith('60') else 'sz'
            if stock_code.startswith('0000') : exchange = 'sh'
            full_code = f"{exchange}.{stock_code}"

            rs = self.bs().query_profit_data(
                full_code,
                year,
                quarter
            )

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(data_list, columns=rs.fields)

            #处理Baostock返回的code前的交易所编码
            if not df.empty:
                df['code'] = df['code'].str.split('.').str[-1] 
                df['ts'] = pd.to_datetime(df['statDate'])
                df = df[['ts', 'code', 'statDate', 'pubDate', 'roeAvg', 'npMargin','gpMargin',
                        'netProfit','epsTTM','MBRevenue','totalShare','liqaShare']]
                df.set_index('ts', inplace=True)
            return df
        
        else:
            logger.error("仅支持从 questdb 或 baostock 获取财务指标")

    def get_stock_industry(self, code=None)-> pd.DataFrame:
        """获取全量行业数据"""
        if self.source == 'questdb':
            if code == None:
                query = f"""
                    SELECT * FROM "stock_industry"
                    LATEST ON ts PARTITION BY code;
                """
            else:
                query = f"""
                    SELECT * FROM stock_industry
                    WHERE code = '{code}'
                    LATEST ON ts;
                """
            error_str = f"提示: QuestDB表 'stock_industry' 不存在"
            return self._read_sql_to_df(query, error_str)

        elif self.source == 'baostock':
            if code != None: logger.warning("code参数无效，从baostock获取行业数据默认为全量数据")
            self.bs = BS()
            rs = self.bs().query_stock_industry()
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(data_list, columns=rs.fields)
            df.rename(columns={'industry': 'industry_name'}, inplace=True)
            df.rename(columns={'industryClassification': 'industry_classification'}, inplace=True)

            #处理Baostock返回的code前的交易所编码
            if not df.empty:
                df['code'] = df['code'].str.split('.').str[-1] 
                df['ts'] = pd.Timestamp.today().date() 
                df = df[['ts', 'code', 'industry_name', 'industry_classification']]
                df.set_index('ts', inplace=True)
            return df
        
        else:
            logger.error("仅支持从 questdb 或 baostock 全量行业数据")

    def get_rolling_financial(self, stock_code, start_date, end_date) -> pd.DataFrame:
        """获取财务指标数据"""
        if self.source == 'questdb':
            query = f"""
                SELECT *
                FROM "rolling_financial"
                    WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{stock_code}'
                    ORDER BY ts
                """
            error_str = f"提示: QuestDB表 'stock_industry' 不存在"
            return self._read_sql_to_df(query, error_str)
        
        elif self.source == 'baostock':
            self.bs = BS()

            # 转换股票代码格式
            exchange = 'sh' if stock_code.startswith('60') else 'sz'
            if stock_code.startswith('0000') : exchange = 'sh'
            full_code = f"{exchange}.{stock_code}"
            
            rs = self.bs().query_history_k_data_plus(
                full_code,
                "date,code,peTTM,psTTM,pcfNcfTTM,pbMRQ",
                start_date,
                end_date
            )

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(data_list, columns=rs.fields)

            #处理Baostock返回的code前的交易所编码
            if not df.empty:
                df['code'] = df['code'].str.split('.').str[-1] 
                df['ts'] = pd.to_datetime(df['date'])
                df = df[['ts', 'code', 'peTTM', 'psTTM', 'pcfNcfTTM', 'pbMRQ']]
                df.set_index('ts', inplace=True)
            return df
        
        else:
            logger.error("仅支持从 questdb 或 baostock 滚动财务指标数")

    def get_concept_boards(self) -> pd.DataFrame:
        """
        获取所有概念板块信息
        返回DataFrame包含字段: ['board_code', 'board_name']
        """
        if self.source == 'questdb':
            query = """
                SELECT *
                FROM "concept_boards"
                LATEST ON ts PARTITION BY code;
            """
            error_str = f"提示: QuestDB表 'concept_boards' 不存在"
            return self._read_sql_to_df(query, error_str)

        elif self.source == 'akshare':
            try:
                df = ak.stock_board_concept_name_ths()
                # 数据清洗
                df['ts'] = pd.Timestamp.today().date() 
                df = df[['ts', 'code', 'name']]
                df.set_index('ts', inplace=True)
                return df
            except Exception as e:
                logger.error(f"获取概念板块数据失败: {e}")
                return pd.DataFrame()
        else:
            logger.error("概念板块数据仅支持akshare或questdb数据源")
            return pd.DataFrame()

    def get_concept_boards_ohlc(self, board_name, start_date, end_date) -> pd.DataFrame:
        """
        获取指定概念板块的日线行情
        返回DataFrame包含字段: ['ts', 'open', 'high', 'low', 'close', 'volume', 'amount']
        """
        board_code = self.get_board_code_by_name(board_name)
        if board_code == None: logger.error(f"未找到概念板块名称 {board_name} 对应的板块代码")

        if self.source == 'questdb':
            query = f"""
                SELECT *
                FROM "concept_boards_ohlc"
                WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{board_code}'
                ORDER BY ts
            """
            error_str = f"提示: QuestDB表 'concept_boards_ohlc' 不存在"
            return self._read_sql_to_df(query, error_str)

        
        elif self.source == 'akshare':
            try:
                df = ak.stock_board_concept_index_ths(symbol=board_name, start_date=start_date, end_date=end_date)
                # 数据清洗
                df = df[['日期', '开盘价', '最高价', '最低价', '收盘价', '成交量', '成交额']]
                df.columns = ['ts', 'open', 'high', 'low', 'close', 'volume', 'amount']
                df['code'] = board_code
                df['name'] = board_name
                df = df[['ts', 'code', 'name', 'open', 'high', 'low', 'close', 'volume', 'amount']]
                return df
            except Exception as e:
                logger.error(f"获取概念板块 {board_code}:{board_name} 实时行情失败: {e}")
                return pd.DataFrame()
        else:
            logger.error("概念板块实时行情仅支持akshare或questdb数据源")
            return pd.DataFrame()
   
    def get_concept_constituents(self, board_name) -> pd.DataFrame:
        """
        获取指定概念板块的成份股
        """
        board_code = self.get_board_code_by_name(board_name)
        if board_code == None: logger.error(f"未找到概念板块名称 {board_name} 对应的板块代码")

        if self.source == 'questdb':
            query = f"""
                SELECT *
                FROM "concept_constituents"
                WHERE board_code = '{board_code}'
                LATEST ON ts PARTITION BY symbol;
            """
            error_str = f"提示: QuestDB表 'concept_constituents' 不存在"
            return self._read_sql_to_df(query, error_str)
        
        elif self.source == 'akshare':
            try:
                df = ak.stock_board_concept_cons_em(symbol=board_name)
                # 数据清洗
                df = df[['代码', '名称']]
                df.columns = ['stock_code', 'stock_name']
                df['board_code'] = board_code
                df['board_name'] = board_name
                df['ts'] = pd.Timestamp.today().date() 
                df = df[['ts', 'board_code', 'board_name', 'stock_code', 'stock_name']]
                return df
            except Exception as e:
                logger.error(f"获取概念板块[{board_name}]成份股失败: {e}")
                return pd.DataFrame()
        else:
            logger.error("概念板块成份股仅支持akshare或questdb数据源")
            return pd.DataFrame()

    def get_board_code_by_name(self, board_name) -> pd.DataFrame:
        """
        通过板块名称获取板块代码
        优先从数据库获取，如未获取到数据，则用get_concept_boards获取全量数据再查找
        如都无数据，则返回null
        
        :param board_name: 板块名称
        :return: 板块代码 (字符串) 或 None
        """

        # 1. 先尝试从数据库查询   
        query = f"""
            SELECT code 
            FROM "concept_boards" 
            WHERE name = '{board_name}'
            LATEST ON ts PARTITION BY code;
        """
        try:
            df = pd.read_sql(query, self.db.pd_conn)
            if not df.empty:
                return df['code'].iloc[0]
        except Exception as e:
            # 表不存在或其他错误
            logger.warning(f"数据库查询失败: {e}")
        
        # 2. 检查数据库中的最新更新时间
        today = datetime.now().date()
        try:
            latest_ts = pd.to_datetime(df['ts'].iloc[0]).date()
            if latest_ts == today:
                # 今天已更新过数据库但仍未找到，直接返回None
                return None
        except:
            pass  # 忽略日期查询错误
        
        # 3. 从akshare获取全量数据
        df = self.get_concept_boards()
        if df is None or df.empty: return None

        self.update_concept_boards(df)
        # 4. 在DataFrame中查找匹配的板块
        matched = df[df['name'] == board_name]
        if not matched.empty: return matched['code'].iloc[0]
        
        # 5. 所有途径都未找到
        return None

    def update_ohlc(self, data:pd.DataFrame, freq):
        """
        将数据存储到QuestDB
        
        :param df: 包含OHLCV数据的DataFrame
        :param stock_code: 股票代码
        :param freq: 数据频率
        """
        if data.empty:
            return
            
        table_name = questDB.get_ohlc_table_name(freq)
                   
        # 插入新数据
        if freq.endswith('min'):
            insert_query = f"""
            INSERT INTO "{table_name}" (ts, open, high, low, close, volume, amount, code, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'MLCN')
            """
            records = [
                (ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['open'], row['high'], row['low'], row['close'], row['volume'], row['amount'], row['code'])
                for ts, row in data.iterrows()
            ]
        else:
            insert_query = f"""
            INSERT INTO "{table_name}" (ts, open, high, low, close, volume, amount, turnover, code, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s ,%s, %s, 'MLCN')
            """
            records = [
                (ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['open'], row['high'], row['low'], row['close'], row['volume'], row['amount'], row['turnover'], row['code'])
                for ts, row in data.iterrows()
            ]
        self.db.batch_insert(insert_query, records)

    def update_stock_basic(self, data:pd.DataFrame):
        """批量更新股票基本信息"""
        query = """
        INSERT INTO stock_basic_info 
        (ts, code, name, ipoDate, outDate, type, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        records = [
                (ts.strftime('%Y-%m-%d %H:%M:%S'), row['code'],
                row['ts'], row['name'], row['ipoDate'], row['outDate'], row['type'], row['status'])
                for ts, row in data.iterrows()
            ]
        return self.db.batch_insert(query, records)

    def update_financial_indicators(self, data:pd.DataFrame):
        """批量更新财务指标"""
        query = """
        INSERT INTO financial_indicators 
        (ts, code, statDate, pubDate, roeAvg, npMargin, gpMargin, netProfit, 
         epsTTM, MBRevenue, totalShare, liqaShare)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), row['code'], 
                row['statDate'], row['pubDate'], row['roeAvg'], row['npMargin'], row['gpMargin'], row['netProfit'],
                row['epsTTM'], row['MBRevenue'], row['otalShare'], row['liqaShare']
                )
                for ts, row in data.iterrows()
            ]

        return self.db.batch_insert(query, records)

    def update_stock_industry(self, data:pd.DataFrame):
        """批量更新行业信息"""
        query = """
        INSERT INTO stock_industry 
        (ts, code, industry_name, industry_classification)
        VALUES (%s, %s, %s, %s)
        """
        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), row['code'], 
                row['industry_name'], row['industry_classification']
                )
                for ts, row in data.iterrows()
            ]

        return self.db.batch_insert(query, records)
    
    def update_rolling_financial(self, data:pd.DataFrame):
        """批量更新财务指标"""
        query = """
        INSERT INTO rolling_financial 
        (ts, code, peTTM, psTTM, pcfNcfTTM, pbMRQ)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), row['code'], 
                row['peTTM'], row['psTTM'], row['pcfNcfTTM'], row['pbMRQ']
                )
                for ts, row in data.iterrows()
            ]

        return self.db.batch_insert(query, records)

    def update_concept_boards(self, data:pd.DataFrame):
        # 插入数据
        insert_query = """
        INSERT INTO "concept_boards" 
        (ts, code, name)
        VALUES (%s, %s, %s)
        """
        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['code'], row['name']
                )
                for ts, row in data.iterrows()
            ]
        
        self.db.batch_insert(insert_query, records)
        logger.info(f"更新概念板块信息成功，共更新{len(records)}条记录")

    def update_concept_boards_ohlc(self, data:pd.DataFrame):
        # 插入数据
        insert_query = """
        INSERT INTO "concept_boards_ohlc" 
        ('ts', 'code', 'name', 'open', 'high', 'low', 'close', 'volume', 'amount')
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['code'], row['name'], row['open'], row['high'], row['low'], row['close'], row['volume'], row['amount']
                )
                for ts, row in data.iterrows()
            ]
        
        self.db.batch_insert(insert_query, records)
        logger.info(f"更新概念板块ohlc数据成功，共更新{len(records)}条记录") 

    def update_concept_constituents(self, board_name, data:pd.DataFrame):
        """
        更新概念板块成份股到数据库
        """

        # 插入数据
        insert_query = """
        INSERT INTO "concept_constituents" 
        (ts, board_name, board_code, stock_name, stock_code)
        VALUES (%s, %s, %s, %s, %s)
        """

        records = [
                (
                ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['board_name'], row['board_code'], row['stock_name'], row['stock_code']
                )
                for ts, row in data.iterrows()
            ]
        
        self.db.batch_insert(insert_query, records)
        logger.info(f"更新[{board_name}]成份股成功，共更新{len(records)}条记录") 

    def _create_ohlc_table(self):
        # 创建OHLC表
        freqs = ['1min', '5min', '15min', '30min', '60min', 'd', 'w', 'm']
        partition_map = {
            '1min': 'HOUR',
            '5min': 'HOUR',
            '15min': 'DAY',
            '30min': 'DAY',
            '60min': 'DAY',
            'd': 'MONTH',
            'w': 'MONTH',
            'm': 'YEAR'
        }
        
        for freq in freqs:
            table_name = questDB.get_ohlc_table_name(freq)
            if freq.endswith('min'):
                create_table = f"""
                CREATE TABLE IF NOT EXISTS "{table_name}" (
                    ts TIMESTAMP,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume LONG,
                    amount LONG,
                    code SYMBOL,
                    market SYMBOL
                ) TIMESTAMP(ts) PARTITION BY {partition_map[freq]} WAL
                DEDUP UPSERT KEYS(ts, code, market);
                """
            else:
                create_table = f"""
                CREATE TABLE IF NOT EXISTS "{table_name}" (
                    ts TIMESTAMP,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume LONG,
                    amount LONG,
                    turnover DOUBLE,
                    code SYMBOL,
                    market SYMBOL
                ) TIMESTAMP(ts) PARTITION BY {partition_map[freq]} WAL
                DEDUP UPSERT KEYS(ts, code, market);
                """
            self.db.create_table(create_table)
  
    def _create_stock_basic_table(self):
        """创建股票基本信息表
        ts: 更新时间
        code: 证券代码
        name: 证券名称
        ipoDate: 上市日期
        outDate: 退市日期
        type: 证券类型 - 1.股票, 2.指数, 3.其它, 4.可转债, 5.ETF
        status: 上市状态 - 1.上市, 0.退市
        """
        create_table = """
        CREATE TABLE IF NOT EXISTS "stock_basic_info" (
            ts TIMESTAMP,
            code SYMBOL,
            name STRING,
            ipoDate TIMESTAMP,
            outDate TIMESTAMP,
            type int,
            status int
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(code, ts);
        """
        return self.db.create_table(create_table)

    def _create_financial_indicators_table(self):
        """创建财务指标表
        pubDate: 公司发布财报的日期
        statDate: 财报统计的季度的最后一天, 比如2017-03-31, 2017-06-30
        roeAvg: 净资产收益率(平均)(%) - 归属母公司股东净利润/[(期初归属母公司股东的权益+期末归属母公司股东的权益)/2]*100%
        npMargin: 销售净利率(%) - 净利润/营业收入*100%
        gpMargin: 销售毛利率(%) - 毛利/营业收入*100% = (营业收入-营业成本)/营业收入*100%
        netProfit: 净利润(元)
        epsTTM: 每股收益 - 归属母公司股东的净利润TTM/最新总股本
        MBRevenue: 主营营业收入(元)
        totalShare: 总股本
        liqaShare: 流通股本
        """
        create_table = """
        CREATE TABLE IF NOT EXISTS "financial_indicators" (
            ts TIMESTAMP,
            code SYMBOL,
            statDate STRING,
            pubDate STRING,
            roeAvg DOUBLE,
            npMargin DOUBLE,
            gpMargin DOUBLE,
            netProfit DOUBLE,
            epsTTM DOUBLE,
            MBRevenue DOUBLE,
            totalShare DOUBLE,
            liqaShare DOUBLE
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(code, ts);
        """
        return self.db.create_table(create_table)

    def _create_stock_industry_table(self):
        """创建行业信息表
        code: 证券代码
        industry: 所属行业
        industryClassification: 所属行业类别
        """
        create_table = """
        CREATE TABLE IF NOT EXISTS "stock_industry" (
            ts TIMESTAMP,
            code SYMBOL,
            industry_name STRING,
            industry_classification STRING
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(code, ts);
        """
        return self.db.create_table(create_table)

    def _create_rolling_financial_table(self):
        """创建行业信息表
        code: 证券代码
        peTTM: 滚动市盈率
        psTTM: 滚动市销率
        pcfNcfTTM: 滚动市现率
        pbMRQ: 市净率
        """
        create_table = """
        CREATE TABLE IF NOT EXISTS "rolling_financial" (
            ts TIMESTAMP,
            code SYMBOL,
            peTTM DOUBLE,
            psTTM DOUBLE,
            pcfNcfTTM DOUBLE,
            pbMRQ DOUBLE
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(code, ts);
        """
        return self.db.create_table(create_table)

    def _create_concept_constituents_table(self):
        """创建成份股表"""
        create_table = """
        CREATE TABLE IF NOT EXISTS "concept_constituents" (
            ts TIMESTAMP,
            board_name STRING,
            board_code SYMBOL,
            stock_name STRING,
            stock_code SYMBOL
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, board_code, stock_code);
        """
        self.db.create_table(create_table)

    def _create_concept_boards_table(self):
        """创建概念板块表"""
        create_table = """
        CREATE TABLE IF NOT EXISTS "concept_boards" (
            ts TIMESTAMP,
            code SYMBOL,
            name STRING,
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(ts, code);
        """
        self.db.create_table(create_table)
       
    def _create_concept_ohlc_table(self):
        """创建概念板块日线数据表"""
        create_table = """
        CREATE TABLE IF NOT EXISTS "concept_boards" (
            ts TIMESTAMP,
            code SYMBOL,
            name STRING,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, code);
        """
        self.db.create_table(create_table)

    def _read_sql_to_df(self, query, error_str = ''):
        try:
            df = pd.read_sql(query, self.db.pd_conn)
            # 数据清洗
            if not df.empty:
                df['ts'] = pd.to_datetime(df['ts'])
                df.set_index('ts', inplace=True)
                return df
        except Exception as e:
            # 表不存在错误代码: 42P01
            if '4xp6' in str(e):logger.error(error_str)
        raise RuntimeError(f"QuestDB数据获取失败: {e}")


# 使用示例
if __name__ == "__main__":
    # x = DataFetcher()
    # data = x.get_rolling_financial('603286',"2024-01-01","2025-01-01")
    # print(data.head())
    x = ak.stock_board_concept_index_ths('阿里巴巴概念', '20200101', '20210101') 
    print(x)