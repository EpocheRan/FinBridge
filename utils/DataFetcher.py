"""
从数据库和第三方接口获取数据(前复权)，并转换成dataframe
"""

import pandas as pd
from datetime import datetime, timedelta
import time
import logging
import akshare as ak

# utils
from utils import *
from utils.DataConnect import questDB, BS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Data Fetcher')

class DataFetcher:
    def __init__(self, source='questdb'):
        '''
        初始化数据获取器
        :param source: 数据源 ('akshare', 'baostock', 'questdb')
        '''
        self.source = source

    def get_ohlc(self, stock_code, start_date, end_date, freq='d'):
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
    
    def _fetch_from_akshare(self, stock_code, start_date, end_date, freq):
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
                        df.set_index('ts', inplace=True)
                        return df
                    except Exception as e:
                        if attempt < max_retry - 1:
                            logger.error(f"akshare数据获取失败 (尝试 {attempt+1}/{max_retry}), 等待 {retry_delay}秒后重试...")
                            time.sleep(retry_delay)
                        else:
                            raise RuntimeError(f"akshare数据获取失败: {e}")
        
    def _fetch_from_baostock(self, stock_code, start_date, end_date, freq):
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
                df = df[['ts', 'open', 'high', 'low', 'close', 'volume','amount']]
                df.set_index('ts', inplace=True)
            else:
                # 日线及以上只有日期
                df = df[['date', 'open', 'high', 'low', 'close', 'volume','amount','turn']]
                df.rename(columns={'turn': 'turnover'}, inplace=True)
                df['ts'] = pd.to_datetime(df['date'])
                df = df[['ts', 'open', 'high', 'low', 'close', 'volume','amount', 'turnover']]
                df.set_index('ts', inplace=True)
            
            # 转换数据类型
            # df = df.apply(pd.to_numeric, errors='ignore')
            df = df.apply(pd.to_numeric)
            return df
        except Exception as e:
            raise RuntimeError(f"baostock数据获取失败: {e}")
    
    def _fetch_from_questdb(self, stock_code, start_date, end_date, freq):
        """从QuestDB获取数据"""
        try:
            # 根据频率确定表名
            table_name = questDB.get_table_name(freq)
            

            # 构建查询字段 - 所有频率都包含amount，日/周/月额外包含turnover
            if freq in ['d', 'w', 'm']:
                fields = "ts, open, high, low, close, volume, amount, turnover"
            else:
                fields = "ts, open, high, low, close, volume, amount"


            query = f"""
                SELECT {fields}
                FROM "{table_name}"
                WHERE ts BETWEEN '{start_date}' AND '{end_date}' AND code = '{stock_code}'
                ORDER BY ts
            """
            df = pd.read_sql(query, self.db.pd_conn)
            
            # 数据清洗
            if not df.empty:
                df['ts'] = pd.to_datetime(df['ts'])
                df.set_index('ts', inplace=True)
            return df
        except Exception as e:
            # 表不存在错误代码: 42P01
            if '4xp6' in str(e):
                logger.error(f"提示: QuestDB表 '{table_name}' 不存在")
            raise RuntimeError(f"QuestDB数据获取失败: {e}")
    
    @staticmethod
    def store_to_questdb(df, stock_code, freq):
        db = questDB()
        """
        将数据存储到QuestDB
        
        :param df: 包含OHLCV数据的DataFrame
        :param stock_code: 股票代码
        :param freq: 数据频率
        """
        if df.empty:
            return
            
        table_name = questDB.get_table_name(freq)
        conn = None

        # 当Table不存在时，创建Table需要标定Parition
        partition_map = {
            '1min': 'HOUR',
            '5min': 'HOUR',
            '15min': 'DAY',
            '60min': 'DAY',
            'd': 'MONTH',
            'w': 'MONTH',
            'm': 'YEAR'
        }

        # 创建表（如果不存在), 分钟数据无换手率，日，周，月有换手率
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
        db.create_table(create_table)
                   
        # 插入新数据
        if freq.endswith('min'):
            insert_query = f"""
            INSERT INTO "{table_name}" (ts, open, high, low, close, volume, amount, code, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s,'{stock_code}', 'MLCN')
            """
            records = [
                (ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['open'], row['high'], row['low'], row['close'], row['volume'], row['amount'])
                for ts, row in df.iterrows()
            ]
        else:
            insert_query = f"""
            INSERT INTO "{table_name}" (ts, open, high, low, close, volume, amount, turnover, code, market)
            VALUES (%s, %s, %s, %s, %s, %s, %s ,%s, '{stock_code}', 'MLCN')
            """
            records = [
                (ts.strftime('%Y-%m-%d %H:%M:%S'), 
                row['open'], row['high'], row['low'], row['close'], row['volume'], row['amount'], row['turnover'])
                for ts, row in df.iterrows()
            ]
        db.batch_insert(insert_query, records)
    