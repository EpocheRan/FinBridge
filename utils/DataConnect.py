"""
管理和数据库与第三方接口的链接
"""

import psycopg2
import baostock
import os
import json
import logging
from sqlalchemy import create_engine

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Data Connection')

class questDB:
    """数据库管理类，封装数据库连接和操作"""
    def __init__(self):
        # 获取项目根目录（假设config.json在项目根目录）
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        config_path = os.path.join(project_root, "config.json")
        
        self.config = self._load_config(config_path)
        self.conn = None
        self._connect()
    
    def _load_config(self,path):
        '''
        读取配置文件
        '''
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"警告: 配置文件加载失败 - {e}。")
                return False
            return config
        else:
            logger.error("questDB配置文件路径不存在。")

    def _connect(self):
        """连接到数据库"""
        try:
            self.conn = psycopg2.connect(**self.config['questdb'])
            self.conn.autocommit = True
            logger.info("数据库连接成功")
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
    
    def is_connected(self):
        """检查数据库是否连接成功"""
        return self.conn is not None and self.conn.closed == 0
    
    def query(self, query, params=None, fetch_all=True):
        """执行SQL查询"""
        if not self.is_connected():
            logger.error("数据库未连接")
            return None
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, params)
                if fetch_all:
                    return cursor.fetchall()
                return cursor.fetchone()
        except Exception as e:
            logger.error(f"查询执行失败: {e}\n查询: {query}\n参数: {params}")
            return None
    
    def update(self, query, params=None):
        """执行更新操作（INSERT/UPDATE/DELETE）"""
        if not self.is_connected():
            logger.error("数据库未连接")
            return False
        
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, params)
                # self.conn.commit()
                return True
        except Exception as e:
            logger.error(f"更新操作失败: {e}\n查询: {query}\n参数: {params}")
            return False
    
    def create_table(self, query):
        """创建表"""
        if not self.is_connected():
            logger.error("数据库未连接")
            return False
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query)
                logger.info(f"检查表是否存在：{query[35:55]}...")
                return True
        except Exception as e:
            logger.error(f"创建表失败: {e}\n查询: {query}\n")
            return False
        
    def batch_insert(self, query, data):
        """批量插入数据"""
        if not data or not self.is_connected():
            return False
        try:
            with self.conn.cursor() as cursor:
                cursor.executemany(query, data)
                logger.info(f"成功批量插入 {len(data)} 条数据")
                return True

        except Exception as e:
            logger.error(f"批量插入失败: {e}")
            return False
    
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
        
    def __del__(self):
        self.close()

    @property
    def pd_conn(self):
        config_map = self.config['questdb']
        return create_engine(f"""postgresql+psycopg2://{config_map["user"]}:{config_map["password"]}@{config_map["host"]}:{config_map["port"]}/{config_map["database"]}""")

    @staticmethod
    def get_ohlc_table_name(freq):
        """根据股票代码和频率生成表名"""
        
        # 有效的频率后缀
        valid_freq = ['1min', '5min', '30min', '15min', '60min', 'd', 'w', 'm']
        
        # 检查频率是否有效
        if freq not in valid_freq:
            freq = 'd'
            logger.info(f"警告: 未知频率 '{freq}', 使用默认频率 'd'")
        
        # 表名称映射
        tableName = {
            '1min':'ohlcv_1m',
            '5min':'ohlcv_5m',
            '15min':'ohlcv_15m',
            '30min':'ohlcv_30m',
            '60min':'ohlcv_60m',
            'd':'ohlcv_1d',
            'w':'ohlcv_1w',
            'm':'ohlcv_1mo'
        }

        return tableName[freq]


class BS():
    """Baostock API管理类，封装Baostock操作"""
    def __init__(self):
        self.connection = None
        self._login()
    
    def _login(self):
        """登录Baostock"""
        try:
            self.connection = baostock
            lg = self.connection.login()
            if lg.error_code == '0':
                logger.info("Baostock登录成功")
                pass
            else:
                logger.error(f"Baostock登录失败: {lg.error_msg}")
        except ImportError:
            logger.error("未安装baostock库，请使用 'pip install baostock' 安装")
            self.connection = None
        except Exception as e:
            logger.error(f"Baostock登录异常: {e}")
            self.connection = None
    
    def __call__(self):
        return self.connection
    
    def is_logged_in(self):
        """检查是否已登录"""
        return self.connection is not None
    
    def logout(self):
        """登出Baostock"""
        if self.connection:
            self.connection.logout()
            logger.info("Baostock已登出")
    
    def __del__(self):
        self.logout()

