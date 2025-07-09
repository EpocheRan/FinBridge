"""
集成各个源获取的数据，并补全数据库数据
"""

import pandas as pd
import logging

# utils
# from .TradingDateManage import TradingDateManage as TDM
# from .DataFetcher import DataFetcher

from TradingDateManage import TradingDateManage as TDM
from DataFetcher import DataFetcher

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Data Loader')

class DataLoader:
    def __init__(self):
        pass

    def _get_trading_days(self, start_date, end_date):
        """
        获取两个日期之间的所有交易日
        
        :param start_date: 开始日期 (YYYY-MM-DD)
        :param end_date: 结束日期 (YYYY-MM-DD)
        :return: 交易日列表
        """
        tdm = TDM()
        return tdm.get_trading_days(start_date, end_date)      

    def _calculate_expected_bars_per_day(self, freq):
        """
        计算不同频率下每天预期的数据条数
        
        :param freq: 数据频率
        :return: 每天预期的数据条数
        """
        if freq == 'd':  # 日线
            return 1
        elif freq.endswith('min'):  # 分钟线
            minutes_per_day = 240  # 4小时交易时间
            minutes_per_bar = int(freq.replace('min', ''))
            return minutes_per_day // minutes_per_bar
        # 周/月线不适用每日条数
        return None

    def _get_expected_dates(self, start_date, end_date, freq):
        """
        获取不同频率下的预期日期/时间点
        
        :param start_date: 开始日期 (YYYY-MM-DD)
        :param end_date: 结束日期 (YYYY-MM-DD)
        :param freq: 数据频率
        :return: 预期日期列表或日期范围
        """
        trading_days = self._get_trading_days(start_date, end_date)
        
        if freq == 'd':  # 日线
            return trading_days
            
        elif freq.endswith('min'):  # 分钟线
            # 返回日期范围，不计算具体分钟点
            return trading_days
            
        elif freq == 'w':  # 周线
            # 获取每周最后一个交易日
            weekly_days = []
            trading_days_dt = pd.to_datetime(trading_days)
            for year, week in set([(d.year, d.week) for d in trading_days_dt]):
                week_days = [d for d in trading_days_dt if d.year == year and d.week == week]
                weekly_days.append(max(week_days).strftime('%Y-%m-%d'))
            return weekly_days
            
        elif freq == 'm':  # 月线
            # 获取每月最后一个交易日
            monthly_days = []
            trading_days_dt = pd.to_datetime(trading_days)
            for year, month in set([(d.year, d.month) for d in trading_days_dt]):
                month_days = [d for d in trading_days_dt if d.year == year and d.month == month]
                monthly_days.append(max(month_days).strftime('%Y-%m-%d'))
            return monthly_days
        
        else:
            raise ValueError(f"不支持的频率类型: {freq}")
    
    def _merge_consecutive_trading_days(self, missing_dates, all_trading_days):
        """
        合并连续的交易日为范围
        
        :param missing_dates: 缺失的交易日列表
        :param all_trading_days: 所有交易日列表
        :return: 合并后的日期范围列表 [(start1, end1), (start2, end2), ...]
        """
        if not missing_dates:
            return []
            
        # 创建交易日索引映射
        trading_day_index = {date: idx for idx, date in enumerate(all_trading_days)}
        
        # 对缺失日期排序
        missing_dates_sorted = sorted(missing_dates)
        
        # 合并连续的交易日
        merged_ranges = []
        current_start = missing_dates_sorted[0]
        current_end = missing_dates_sorted[0]
        prev_index = trading_day_index[current_start]
        
        for date in missing_dates_sorted[1:]:
            current_index = trading_day_index[date]
            
            # 检查是否连续（当前索引 = 上一个索引 + 1）
            if current_index == prev_index + 1:
                current_end = date
            else:
                # 结束当前范围，开始新范围
                merged_ranges.append((current_start, current_end))
                current_start = date
                current_end = date
            
            prev_index = current_index
        
        # 添加最后一个范围
        merged_ranges.append((current_start, current_end))
        
        return merged_ranges


    def ohlc(self, stock_code, start_date, end_date, freq='d'):
        """
        获取股票数据（智能增量更新）

        freq ['1min', '5min', '15min', '30min', '60min', 'd', 'w', 'm']
        """
        # 1. 尝试从QuestDB获取完整数据
        questdb_fetcher = DataFetcher(source='questdb')
        qurey_end_date = end_date + " 23:59"
        try:
            local_df = questdb_fetcher.get_ohlc(stock_code, start_date, qurey_end_date, freq)
            logger.info(f"从QuestDB获取到 {stock_code} {freq} 数据 ({len(local_df)} 行)")
        except Exception as e:
            logger.error(f"QuestDB查询异常: {e}")
            local_df = pd.DataFrame()

        # 2. 获取预期日期/时间点
        try:
            expected_dates = self._get_expected_dates(start_date, end_date, freq)
            if not expected_dates:
                logger.warning("未获取到预期日期数据")
                return local_df if not local_df.empty else pd.DataFrame()
        except Exception as e:
            logger.error(f"生成预期日期失败: {e}")
            expected_dates = []
        
        # 3. 检查本地数据的完整性（根据不同频率采用不同策略）
        missing_dates = []
        missing_date_ranges = []
        all_trading_days = self._get_trading_days(start_date, end_date)  # 所有交易日
        
        if freq.endswith('min'):  # 分钟级数据策略
            # 计算每天预期的分钟数据条数
            expected_bars_per_day = self._calculate_expected_bars_per_day(freq)
            
            if not local_df.empty:
                # 提取日期部分（去除时间）
                local_df['date'] = local_df.index.strftime('%Y-%m-%d')
                
                # 按日期分组并计算每天的数据量
                daily_counts = local_df.groupby('date').size()
                
                # 检查每天的数据量是否达到预期
                for date in expected_dates:
                    if date in daily_counts.index:
                        if daily_counts[date] < expected_bars_per_day:
                            missing_dates.append(date)
                            logger.warning(f"{date} 数据不足: 预期 {expected_bars_per_day} 条, 实际 {daily_counts[date]} 条")
                    else:
                        missing_dates.append(date)
                        logger.warning(f"{date} 数据缺失")
            else:
                missing_dates = expected_dates  # 全量缺失
                
            # 将连续缺失交易日合并为范围
            if missing_dates:
                missing_date_ranges = self._merge_consecutive_trading_days(missing_dates, all_trading_days)
                
        elif freq == 'd':  # 日线数据策略
            # 检查缺失日期
            if not local_df.empty:
                local_dates = local_df.index.strftime('%Y-%m-%d').tolist()
                missing_dates = [date for date in expected_dates if date not in local_dates]
            else:
                missing_dates = expected_dates
                
            # 将连续缺失交易日合并为范围
            if missing_dates:
                missing_date_ranges = self._merge_consecutive_trading_days(missing_dates, all_trading_days)
                
        else:  # 周线/月线策略
            # 检查关键时间点
            if not local_df.empty:
                local_dates = local_df.index.strftime('%Y-%m-%d').tolist()
                missing_dates = [date for date in expected_dates if date not in local_dates]
            else:
                missing_dates = expected_dates
            
            # 对于周/月线，直接取整个缺失范围
            if missing_dates:
                start = min(missing_dates)
                end = max(missing_dates)
                missing_date_ranges = [(start, end)]
        
        # 4. 从API获取缺失数据
        api_df = pd.DataFrame()
        if missing_date_ranges:
            logger.info(f"发现 {len(missing_dates)} 个缺失时间点，合并为 {len(missing_date_ranges)} 个范围")
            
            # 确定数据源, Baostock不包含1min数据，从Akshare获取
            # 改进点：多个数据源时用while来遍历直至 not api_df.empty == true
            if stock_code.startswith(("51", "15")):
                logger.info(f"从akshare获取ETF {stock_code} , {freq} 数据")
                fetcher = DataFetcher(source='akshare')
            else:
                if freq.endswith('1min'):
                    logger.info(f"从akshare获取 {stock_code} , {freq} 数据")
                    fetcher = DataFetcher(source='akshare')
                else:
                    logger.info(f"从Baostock获取 {stock_code} , {freq} 数据")
                    fetcher = DataFetcher(source='baostock')
                
            # 按范围获取缺失数据
            for start, end in missing_date_ranges:
                logger.info(f"获取 {start} 至 {end} 的数据")
                partial_df = fetcher.get_ohlc(stock_code, start, end, freq)
                if not partial_df.empty:
                    api_df = pd.concat([api_df, partial_df])
        
        # 5. 存储增量数据到数据库
        if not api_df.empty:
            logger.info(f"将增量数据存储到QuestDB ({len(api_df)} 行)")
            questdb_fetcher.update_ohlc(api_df, stock_code, freq)
            
            # 合并数据
            if not local_df.empty:
                combined_df = pd.concat([local_df, api_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep='first')]
                combined_df = combined_df.sort_index()
            else:
                combined_df = api_df
        else:
            combined_df = local_df if not local_df.empty else pd.DataFrame()
            logger.warning("无增量数据可存储")
        
        # 6. 返回最终数据
        logger.info(
            f"数据获取完成: 本地 {len(local_df)} 条 + 增量 {len(api_df)} 条 = 总计 {len(combined_df)} 条"
        )
        return combined_df
    
    def rolling_finance(self, stock_code, start_date, end_date):

        # 1. 尝试从QuestDB获取完整数据
        questdb_fetcher = DataFetcher(source='questdb')
        qurey_end_date = end_date + " 23:59"
        try:
            local_df = questdb_fetcher.get_rolling_financial(stock_code, start_date, qurey_end_date)
            logger.info(f"从QuestDB获取到 {stock_code} 滚动财务指标 ({len(local_df)} 行)")
        except Exception as e:
            logger.error(f"QuestDB查询异常: {e}")
            local_df = pd.DataFrame()

        # 2. 获取预期日期/时间点
        try:
            expected_dates = self._get_expected_dates(start_date, end_date, 'd')
            if not expected_dates:
                logger.warning("未获取到预期日期数据")
                return local_df if not local_df.empty else pd.DataFrame()
        except Exception as e:
            logger.error(f"生成预期日期失败: {e}")
            expected_dates = []
        
        # 3. 检查本地数据的完整性
        missing_dates = []
        missing_date_ranges = []
        all_trading_days = self._get_trading_days(start_date, end_date)  # 所有交易日
        # 检查缺失日期
        if not local_df.empty:
            local_dates = local_df.index.strftime('%Y-%m-%d').tolist()
            missing_dates = [date for date in expected_dates if date not in local_dates]
        else:
            missing_dates = expected_dates
            
        # 将连续缺失交易日合并为范围
        if missing_dates:
            missing_date_ranges = self._merge_consecutive_trading_days(missing_dates, all_trading_days)
        
        # 4. 从API获取缺失数据
        api_df = pd.DataFrame()
        if missing_date_ranges:
            logger.info(f"发现 {len(missing_dates)} 个缺失时间点，合并为 {len(missing_date_ranges)} 个范围")
            logger.info(f"从Baostock获取 {stock_code} , 滚动财务指标")
            baostock_fetcher = DataFetcher(source='baostock')

            # 按范围获取缺失数据
            for start, end in missing_date_ranges:
                logger.info(f"获取 {start} 至 {end} 的数据")
                partial_df = baostock_fetcher.get_rolling_financial(stock_code, start, end)
                if not partial_df.empty:
                    api_df = pd.concat([api_df, partial_df])    

        # 5. 存储增量数据到数据库
        if not api_df.empty:
            logger.info(f"将增量数据存储到QuestDB ({len(api_df)} 行)")
            questdb_fetcher.update_rolling_financial_table(api_df)   
            
            # 合并数据
            if not local_df.empty:
                combined_df = pd.concat([local_df, api_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep='first')]
                combined_df = combined_df.sort_index()
            else:
                combined_df = api_df

        else:
            combined_df = local_df if not local_df.empty else pd.DataFrame()
            logger.warning("无增量数据可存储")    
        
        return combined_df 

# 使用示例
if __name__ == "__main__":
    loader = DataLoader()
    data = loader.get_rolling_finance_data("603286", "2024-01-01", "2025-06-10")
    print(data.head())