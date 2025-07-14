"""
集成各个源获取的数据，并补全数据库数据
"""

import pandas as pd
import logging
from abc import ABC, abstractmethod
import calendar
from datetime import datetime
from tqdm import tqdm # 还没想好怎么用

# utils
# from .TradingDateManage import TradingDateManage as TDM
# from .DataFetcher import DataFetcher

from TradingDateManage import TradingDateManage as TDM
from DataFetcher import DataFetcher

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Data Loader')

class DataUpdateTool(ABC):
    """数据读取与更新策略的抽象基类"""
    
    @abstractmethod
    def get_expected_dates(self, loader, start_date:str, end_date:str, freq):
        """获取预期日期范围"""
        pass
    
    @abstractmethod
    def update_database(self, db_fetcher:DataFetcher, data:pd.DataFrame, freq=None):
        """更新数据库"""
        pass
    
    @abstractmethod
    def get_data(self, fetcher:DataFetcher, stock_code:str, start_date:str, end_date:str, freq=None):
        """从本地数据库获取数据"""
        pass

class OHLCUpdateTool(DataUpdateTool):
    """OHLC数据读取与更新策略"""
    
    def get_expected_dates(self, loader, start_date, end_date, freq):
        """获取OHLC数据的预期日期"""
        return loader._get_expected_dates(start_date, end_date, freq)
    
    def update_database(self, db_fetcher, data, freq):
        """更新OHLC数据库"""
        db_fetcher.update_ohlc(data, freq)
    
    def get_data(self, fetcher, stock_code, start_date, end_date, freq):
        """获取本地OHLC数据"""
        return fetcher.get_ohlc(stock_code, start_date, end_date, freq)

class RollingFinanceTool(DataUpdateTool):
    """财务数据读取与更新策略"""
    
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        """获取财务数据的预期日期"""
        # 财务数据使用日线频率
        return loader._get_expected_dates(start_date, end_date, 'd')
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新财务数据库"""
        db_fetcher.update_rolling_financial(data)
    
    def get_data(self, fetcher, stock_code, start_date, end_date, freq=None):
        """获取本地财务数据"""
        return fetcher.get_rolling_financial(stock_code, start_date, end_date)

class BasicInfoTool(DataUpdateTool):
    """基本信息读取与更新"""

    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        logger.warning("基础信息无日期范围，该方法仅返回日线数据")
        return loader._get_expected_dates(start_date, end_date, 'd') 
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新数据库"""
        db_fetcher.update_stock_basic(data)
    
    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        """获取数据"""
        return fetcher.get_stock_basic_info()
    
class FinancialIndicatorsTool(DataUpdateTool):
    """基础财务指标读取与更新"""

    def get_expected_dates(self, loader, start_date, end_date=None, freq=None):
        logger.warning("基础财务指标返回年与季度信息year：str, quarter:str")
        return loader._get_quarter_info(start_date)
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新数据库"""
        db_fetcher.update_stock_basic(data)
    
    def get_data(self, fetcher, stock_code, start_date, end_date, freq=None):
        """获取数据"""
        # start_date: year
        # end_date: quarter
        return fetcher.get_financial_indicators(stock_code, start_date, end_date)
    
class StockIndustryTool(DataUpdateTool):
    """行业数据"""

    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        logger.warning("基础信息无日期范围，该方法仅返回日线数据")
        return loader._get_expected_dates(start_date, end_date, 'd') 
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新数据库"""
        db_fetcher.update_stock_industry(data)
    
    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        """获取数据"""
        return fetcher.get_stock_industry()
    
class ConceptBoardsTool(DataUpdateTool):
    """基本信息"""

    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        logger.warning("基础信息无日期范围，该方法仅返回日线数据")
        return loader._get_expected_dates(start_date, end_date, 'd') 
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新数据库"""
        db_fetcher.update_concept_boards(data)
    
    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        """获取数据"""
        return fetcher.get_concept_boards()

class ConceptBoardsohlcTool(DataUpdateTool):
    """概念指数数据读取与更新工具"""    
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        """获取财务数据的预期日期"""
        # 财务数据使用日线频率
        return loader._get_expected_dates(start_date, end_date, 'd')
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新财务数据库"""
        db_fetcher.update_rolling_financial(data)
    
    def get_data(self, fetcher, stock_code, start_date, end_date, freq=None):
        """获取本地财务数据"""
        return fetcher.get_concept_boards_ohlc(stock_code, start_date, end_date)
    
class ConceptConstituents(DataUpdateTool):
    """概念指数成分股读取与更新工具"""  
    def get_expected_dates(self, loader, start_date, end_date, freq=None):
        logger.warning("基础信息无日期范围，该方法仅返回日线数据")
        return loader._get_expected_dates(start_date, end_date, 'd') 
    
    def update_database(self, db_fetcher, data, freq=None):
        """更新数据库"""
        db_fetcher.update_concept_constituents(data)
    
    def get_data(self, fetcher, stock_code=None, start_date=None, end_date=None, freq=None):
        """获取数据"""
        return fetcher.get_concept_constituents()

class DataLoader():
    def __init__(self):
        # 初始化工具
        self.ohlc_tool = OHLCUpdateTool()
        self.rolling_finance_tool = RollingFinanceTool()
        self.basic_info_tool = BasicInfoTool()
        self.financial_indicators_tool = FinancialIndicatorsTool()
        self.stock_industry_tool = StockIndustryTool()
        self.concept_boards_tool = ConceptBoardsTool()
        self.concept_boards_ohlc_tool = ConceptBoardsohlcTool()
        self.concept_constituents_tool = ConceptConstituents()

    def _get_quarter_info(self, date) -> tuple:
        """将日期转换为(年份, 季度)元组"""
        dt = pd.to_datetime(date)
        year = dt.year
        quarter = (dt.month - 1) // 3 + 1
        return year, quarter

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
            return trading_days, trading_days
            
        elif freq.endswith('min'):  # 分钟线
            # 返回日期范围，不计算具体分钟点
            return trading_days, trading_days
            
        elif freq == 'w':  # 周线
            # 获取每周最后一个交易日
            weekly_days = []
            trading_days_dt = pd.to_datetime(trading_days)
            for year, week in set([(d.year, d.week) for d in trading_days_dt]):
                week_days = [d for d in trading_days_dt if d.year == year and d.week == week]
                weekly_days.append(max(week_days).strftime('%Y-%m-%d'))
            return weekly_days, trading_days
            
        elif freq == 'm':  # 月线
            # 获取每月最后一个交易日
            monthly_days = []
            trading_days_dt = pd.to_datetime(trading_days)
            for year, month in set([(d.year, d.month) for d in trading_days_dt]):
                month_days = [d for d in trading_days_dt if d.year == year and d.month == month]
                monthly_days.append(max(month_days).strftime('%Y-%m-%d'))
            return monthly_days, trading_days
        
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
    
    def _check_missing_data(self, local_df, expected_dates, trading_days, freq):
        """
        检查本地数据的完整性并识别缺失日期范围
        
        :param local_df: 本地获取的DataFrame数据
        :param expected_dates: 预期的日期列表
        :param freq: 数据频率
        :param start_date: 开始日期
        :param end_date: 结束日期
        :return: 缺失日期范围列表 [(start1, end1), (start2, end2), ...]
        """
        if freq == None: freq = 'd'

        missing_dates = []
        missing_date_ranges = []
        
        if freq.endswith('min'):  # 分钟级数据策略
            expected_bars_per_day = self._calculate_expected_bars_per_day(freq)
            
            if not local_df.empty:
                # 提取日期部分
                local_df['date'] = local_df.index.strftime('%Y-%m-%d')
                daily_counts = local_df.groupby('date').size()
                
                for date in expected_dates:
                    if date in daily_counts.index:
                        if daily_counts[date] < expected_bars_per_day:
                            missing_dates.append(date)
                    else:
                        missing_dates.append(date)
            else:
                missing_dates = expected_dates  # 全量缺失
                
            # 合并连续缺失日期
            if missing_dates:
                missing_date_ranges = self._merge_consecutive_trading_days(missing_dates, trading_days)
                
        elif freq == 'd':  # 日线数据
            if not local_df.empty:
                local_dates = local_df.index.strftime('%Y-%m-%d').tolist()
                missing_dates = [date for date in expected_dates if date not in local_dates]
            else:
                missing_dates = expected_dates
                
            if missing_dates:
                missing_date_ranges = self._merge_consecutive_trading_days(missing_dates, trading_days)
                
        else:  # 周线/月线
            if not local_df.empty:
                local_dates = local_df.index.strftime('%Y-%m-%d').tolist()
                missing_dates = [date for date in expected_dates if date not in local_dates]
            else:
                missing_dates = expected_dates
                
            if missing_dates:
                start = min(missing_dates)
                end = max(missing_dates)
                missing_date_ranges = [(start, end)]
        
        return missing_date_ranges

    def _incremental_update(self, stock_code, start_date, end_date, tool:DataUpdateTool, freq=None):
        """
        通用增量更新框架
        
        :param stock_code: 股票代码（在请求concept_boards_ohlc时，stock_code为board_name）
        :param start_date: 开始日期
        :param end_date: 结束日期
        :param tool: 更新工具实例
        :param freq: 数据频率 (仅对OHLC有效)
        :return: 合并后的数据
        """
        # 1. 尝试从QuestDB获取完整数据
        questdb_fetcher = DataFetcher(source='questdb')
        query_end_date = end_date + " 23:59"
        
        try:
            local_df = tool.get_data(
                questdb_fetcher, stock_code, start_date, query_end_date, freq
            )
            logger.info(f"从QuestDB获取到 {stock_code} 数据 ({len(local_df)} 行)")
        except Exception as e:
            logger.error(f"QuestDB查询异常: {e}")
            local_df = pd.DataFrame()

        # 2. 获取预期日期
        try:
            expected_dates, trading_dates = tool.get_expected_dates(self, start_date, end_date, freq)
            if not expected_dates:
                logger.warning("未获取到预期日期数据")
                return local_df if not local_df.empty else pd.DataFrame()
        except Exception as e:
            logger.error(f"生成预期日期失败: {e}")
            expected_dates = []
        
        # 3. 检查缺失数据
        missing_date_ranges = self._check_missing_data(local_df, expected_dates, trading_dates, freq)
        
        # 4. 从API获取缺失数据
        api_df = pd.DataFrame()
        if missing_date_ranges:
            logger.info(f"发现 {len(missing_date_ranges)} 个缺失日期范围")
            
            # 确定数据源
            if stock_code.startswith(("51", "15")) or (freq and freq.endswith('1min')):
                logger.info(f"从akshare获取 {stock_code} 数据")
                api_fetcher = DataFetcher(source='akshare')
            else:
                logger.info(f"从Baostock获取 {stock_code} 数据")
                api_fetcher = DataFetcher(source='baostock')
            
            # 按范围获取缺失数据
            for date_range in missing_date_ranges:
                start, end = date_range
                logger.info(f"获取 {start} 至 {end} 的数据")
                partial_df = tool.get_data(api_fetcher, stock_code, start, end, freq)
                if not partial_df.empty:
                    api_df = pd.concat([api_df, partial_df])
        
        # 5. 存储增量数据到数据库
        if not api_df.empty:
            logger.info(f"将增量数据存储到QuestDB ({len(api_df)} 行)")
            tool.update_database(questdb_fetcher, api_df, freq)
            
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

    def _lastest_tradingday_update(self, tool:DataUpdateTool):
        """更新并返回最新的股票基本信息"""
        from datetime import datetime, timedelta
        
        # 1. 尝试从数据库获取数据
        questdb_fetcher = DataFetcher(source='questdb')
        try:
            db_data = tool.get_data(questdb_fetcher)
            logger.info(f"从数据库获取到股票基本信息 ({len(db_data)} 行)")
        except Exception as e:
            logger.error(f"数据库查询异常: {e}")
            db_data = pd.DataFrame()

        # 2. 获取最新交易日
        try:
            # 获取最近30天的交易日，确保覆盖到最新的交易日
            end_date = datetime.today().strftime('%Y-%m-%d')
            start_date = (datetime.today() - timedelta(days=30)).strftime('%Y-%m-%d')
            
            tdm = TDM()
            trading_days_df = tdm.get_trading_days(start_date, end_date)
            
            if trading_days_df.empty:
                logger.error("无法获取交易日数据")
                # 使用今天作为备选，但通常不是交易日
                latest_trading_day = end_date
            else:
                # 取最后一个交易日
                latest_trading_day = trading_days_df.iloc[-1]['ts']
                
            logger.info(f"最新交易日: {latest_trading_day}")
        except Exception as e:
            logger.error(f"获取交易日失败: {e}")
            latest_trading_day = datetime.today().strftime('%Y-%m-%d')

        # 3. 检查数据库数据是否最新
        if not db_data.empty:
            # 假设数据中有'ts'字段记录最后更新日期
            # 如果没有这个字段，则总是更新
            if 'ts' in db_data.columns:
                last_update = db_data['ts'].max()
                if pd.to_datetime(last_update) >= pd.to_datetime(latest_trading_day):
                    logger.info("数据库数据已是最新")
                    return db_data
            else:
                logger.warning("数据库数据中缺少'ts'字段，将尝试更新")

        # 4. 从API获取最新数据
        logger.info("从API获取最新股票基本信息")
        try:
            api_fetcher = DataFetcher(source='baostock')
            api_data = tool.get_data(api_fetcher)
            
            if not api_data.empty:
                # 添加当前日期作为更新时间戳
                api_data['ts'] = pd.Timestamp.now()
                logger.info(f"从API获取到 {len(api_data)} 行数据")
                
                # 5. 更新数据库
                tool.update_database(questdb_fetcher, api_data)
                logger.info("数据已更新到数据库")
                return api_data
            else:
                logger.warning("API返回空数据")
        except Exception as e:
            logger.error(f"API请求失败: {e}")

        # 6. 返回数据库数据或空DataFrame
        return db_data if not db_data.empty else pd.DataFrame()
    
    def _quarter_update(self, stock_code, start_date, end_date, tool:DataUpdateTool, forecast:bool=True) -> pd.DataFrame:

        """
        返回[start_date, end_date] 区间**每个交易日**对应的财务指标。

        参数
        ----
        stock_code : str
            6 位证券代码（不含交易所前缀）
        start_date : str
            'YYYY-MM-DD'
        end_date   : str
            'YYYY-MM-DD'
        tool       : FinancialIndicatorsTool
            外层已在 DataLoader.__init__ 中构造好： self.financial_indicators_tool
        forecast    : bool
            True  → 取“当季”报；False → 取“上一季”报
        """
        # 获取当前“今天”日期（去掉时分秒）
        today = datetime.now().date()

        # 1. 生成交易日序列 
        trading_days = self._get_trading_days(start_date, end_date)

        # 2. 计算每个交易日 → 需要的 (year, quarter) 以及 quarter_end 
        #    同时搜集所有【唯一】(year, quarter)
        day_quarter_map = {} # Dict[pd.Timestamp, Tuple[int, int, str]], 交易日 → (年, 季度, 季末日期)
        quarters_needed = {} # Dict[Tuple[int, int], str], (年, 季度) → 季末日期（避免重复计算）

        for day in trading_days:  # 遍历所有交易日
            # 获取该交易日所在的年份(y)和季度(q, 1-4)
            y, q = self._get_quarter_info(day)                  

            # 先算该 (y,q) 的季度末日期
            month_end = {1: 3, 2: 6, 3: 9, 4: 12}[q]
            last_day = calendar.monthrange(y, month_end)[1]
            qe_date = datetime(y, month_end, last_day)

            if forecast:
                # 当季报：如果当季季度末在未来（qe_date > today），则回退到上一季
                if qe_date.date() > today:
                    if q > 1:
                        q -= 1
                    else:
                        q, y = 4, y - 1
                    month_end = {1: 3, 2: 6, 3: 9, 4: 12}[q]
                    last_day = calendar.monthrange(y, month_end)[1]
                    qe_date = datetime(y, month_end, last_day)
                # 否则保留当季 (y,q,qe_date)
            else:
                # 历史报：始终取上一季
                if q > 1:
                    q -= 1
                else:
                    q, y = 4, y - 1
                month_end = {1: 3, 2: 6, 3: 9, 4: 12}[q]
                last_day = calendar.monthrange(y, month_end)[1]
                qe_date = datetime(y, month_end, last_day)

            qe_str = qe_date.strftime("%Y-%m-%d")
            day_quarter_map[day] = (y, q, qe_str)
            quarters_needed[(y, q)] = qe_str
            

        # 3. 先尝试从 QuestDB 拉取所有需要的季度 
        db_fetcher = DataFetcher(source="questdb")
        local_parts   = []           # 收集成功读取的数据块
        have_quarters = set()        # 已成功获取 (y,q)

        for (y, q), qe in quarters_needed.items():
            # tool.get_data: (fetcher, code, year, quarter)
            part = tool.get_data(db_fetcher, stock_code, y, q)
            if part is not None and not part.empty:
                local_parts.append(part)
                have_quarters.add((y, q))

        # 4. 对缺失季度调用 Baostock，再写回 QuestDB 
        missing_quarters = [k for k in quarters_needed if k not in have_quarters]
        if missing_quarters:
            api_fetcher = DataFetcher(source="baostock")
            api_parts   = []

            for y, q in missing_quarters:
                part = tool.get_data(api_fetcher, stock_code, y, q)
                if part is not None and not part.empty:
                    api_parts.append(part)

            # 有拉回数据
            if api_parts:                                   
                api_df = pd.concat(api_parts)
                # 写入本地数据库
                tool.update_database(db_fetcher, api_df)
                local_parts.append(api_df)

        # 5️. 合并全部季度财报 & 过滤仅保留需要的季度
        if not local_parts:
            raise RuntimeError("未能获得任何财务数据，请检查数据库 或 API。")

        qdf = pd.concat(local_parts)
        # index 已经是财报发布日期 ts (yyyy‑mm‑dd)
        qdf.index = qdf.index.map(lambda x: pd.to_datetime(x).strftime("%Y-%m-%d"))

        needed_qe_set = set(quarters_needed.values())
        qdf = qdf.loc[qdf.index.intersection(needed_qe_set)].sort_index()

        # 6️. “展开”到每日粒度
        data = pd.DataFrame(index=trading_days)
        for col in qdf.columns:
            data[col] = [
                qdf.at[day_quarter_map[d][2], col]     # day_quarter_map[d][2] 是 qe_str
                for d in trading_days
            ]

        return data

# 下方为数据获取方法

    def ohlc(self, stock_codes: str | list[str], start_date, end_date, freq='d') -> pd.DataFrame:
        if isinstance(stock_codes, str):
            stock_codes = [stock_codes]

        stock_data = {}
        for stock_code in stock_codes:
            stock_data[stock_code] = self._incremental_update(
                                    stock_code, start_date, end_date, self.ohlc_tool, freq)
        return stock_data

    def rolling_finance(self, stock_codes: str | list[str], start_date, end_date) -> pd.DataFrame:
        if isinstance(stock_codes, str):
            stock_codes = [stock_codes]

        stock_data = {}
        for stock_code in stock_codes:
            stock_data[stock_code] =  self._incremental_update(
                                    stock_code, start_date, end_date, self.rolling_finance_tool)
        return stock_data
    
    def stock_basic_info(self) -> pd.DataFrame:
        stock_data =  self._lastest_tradingday_update(self.basic_info_tool)
        return stock_data
    
    def financial_indicators(self, stock_codes: str | list[str], start_date, end_date, forecast=False) -> pd.DataFrame:
        if isinstance(stock_codes, str):
            stock_codes = [stock_codes]

        stock_data = {}
        for stock_code in stock_codes:
            stock_data[stock_code] =  self._quarter_update(
                                    stock_code, start_date, end_date, self.financial_indicators_tool, forecast)
        return stock_data

    def stock_industry(self) -> pd.DataFrame:
        stock_data =  self._lastest_tradingday_update(self.stock_industry_tool)
        return stock_data

    def concept_boards(self) -> pd.DataFrame:
        boards_data =  self._lastest_tradingday_update(self.concept_boards_tool)
        return boards_data

    def concept_boards_ohlc(self, board_name: str | list[str], start_date, end_date) -> pd.DataFrame:
        if isinstance(board_name, str):
            board_name = [board_name]

        stock_data = {}
        for name in board_name:
            stock_data[name] =  self._incremental_update(
                                    name, start_date, end_date, self.concept_boards_ohlc_tool)
        return stock_data

    def concept_constituents(self) -> pd.DataFrame:
        boards_data =  self._lastest_tradingday_update(self.concept_constituents_tool)
        return boards_data

# 使用示例
if __name__ == "__main__":
    loader = DataLoader()
    data = loader.rolling_finance("603282", "2024-01-01", "2025-06-10")
    print(data["603282"].head())