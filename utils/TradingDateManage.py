"""
返回两个日期间交易日的LIST，并自动更新数据库
"""

from datetime import datetime, timedelta
import time
import logging
import pandas as pd

# utils
# from .DataConnect import questDB, BS

from DataConnect import questDB, BS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Trading Date')

class TradingDateManage:
    def __init__(self):
        self.db = None
        self.initialize_table()

    def initialize_table(self):
        """初始化交易日表"""
        logger.info("Initializing trading_days table...")
        create_table = """
            CREATE TABLE IF NOT EXISTS trading_days (
                ts TIMESTAMP,
                is_trading BOOLEAN NOT NULL
            ) TIMESTAMP(ts) PARTITION BY YEAR WAL;
        """
        self.db.create_table(create_table)
        logger.info("trading_days table initialized")
    
    def _get_last_db_date(self):
        """获取数据库中最后的日期"""
        try:
            result = self.db.query('SELECT MAX(ts) FROM trading_days;',fetch_all=False)
            return result[0].strftime('%Y-%m-%d') if result and result[0] else None
        except Exception as e:
            logger.error(f"获取最后日期失败: {e}")
            return None
    
    def _fill_missing_days(self, end_date):
        """补全从数据库最后日期到指定结束日期的交易日数据"""
        last_date = self._get_last_db_date()
        if not last_date:
            start_fill = '1990-01-01'
        else:
            start_fill = (datetime.strptime(last_date, '%Y-%m-%d') + 
                         timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 如果不需要补全则提前返回
        if last_date and start_fill > end_date:
            return
        
        # 获取缺失日期的交易日数据
        missing_days = self._fetch_trading_days(start_fill, end_date)
        if not missing_days:
            return
        
        # 生成所有日期并标记是否为交易日
        all_dates = self._date_range(start_fill, end_date)
        data_to_insert = [
            (date, date in missing_days) 
            for date in all_dates
        ]
        
        # 批量插入数据库 (使用 QuestDB 高性能写入)
        self._batch_insert(data_to_insert)
    
    def _batch_insert(self, data):
        """批量插入数据到 QuestDB"""
        if not data:
            return
        
        query = "INSERT INTO trading_days (ts, is_trading) VALUES "
        values = []
        
        for date_str, is_trading in data:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            values.append(f"(DATE '{date_obj}', {is_trading})")
        
        query += ", ".join(values) + ";"
        self.db.update(query)

    
    def _fetch_trading_days(self, start_date, end_date):
        """从 API 获取交易日数据"""
        try:
            bs = BS()
            rs = bs().query_trade_dates(start_date=start_date, end_date=end_date)
            trading_days = []
            while rs.error_code == '0' and rs.next():
                date, is_trading = rs.get_row_data()
                if is_trading == '1':
                    trading_days.append(date)
            return trading_days
        except Exception as e:
            logger.error(f"API 请求失败: {e}")
            return []
    
    def _date_range(self, start_date, end_date):
        """生成日期范围内的所有日期（YYYY-MM-DD 格式）"""
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        return [
            (start + timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range((end - start).days + 1)
        ]
    
    def get_trading_days(self, start_date, end_date) -> list:
        """获取两个日期之间的交易日（优先使用数据库）"""
        # 确保数据库包含最新数据
        current_date = datetime.now().strftime('%Y-%m-%d')
        self._fill_missing_days(current_date)
        
        # 从数据库查询交易日（利用 QuestDB 的时间序列特性）
        trading_days = self.db.query('''
                SELECT ts
                FROM trading_days 
                WHERE ts BETWEEN %s AND %s 
                AND is_trading = true
                ORDER BY ts;
        ''', (start_date, end_date))
        logger.info('获取交易日成功') 
        return [row[0].strftime('%Y-%m-%d') for row in trading_days]

# 使用示例
if __name__ == "__main__":

    # 测试查询
    start = time.time()
    TDM = TradingDateManage()
    trading_days = TDM.get_trading_days('2023-01-01', '2023-02-01')
    print(f"获取 {len(trading_days)} 个交易日, 耗时: {time.time()-start:.4f}秒")
    print("前5个交易日:", trading_days[:5])