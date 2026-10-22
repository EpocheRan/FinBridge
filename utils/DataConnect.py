"""
管理和数据库与第三方接口的链接
"""

import psycopg2
from psycopg2.extensions import register_adapter, AsIs
import numpy as np
import baostock

import os
import json
import logging
from sqlalchemy import create_engine

# 设置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Data Connection")


# 注册 numpy 类型适配器，以便 psycopg2 能直接处理 numpy 类型
register_adapter(np.float64, AsIs)
register_adapter(np.float32, AsIs)
register_adapter(np.int64, AsIs)
register_adapter(np.int32, AsIs)


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

    def _load_config(self, path):
        """
        读取配置文件
        """
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
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
            self.conn = psycopg2.connect(**self.config["questdb"])
            self.conn.autocommit = True
            # 设置连接参数，避免空闲超时
            self.conn.set_session(autocommit=True, readonly=False)
            logger.info("数据库连接成功")
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")

    def is_connected(self):
        """检查数据库是否连接成功"""
        try:
            if self.conn is None or self.conn.closed != 0:
                return False
            # 尝试执行简单查询验证连接
            with self.conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _ensure_connected(self):
        """确保数据库连接，如果断开则重连"""
        if not self.is_connected():
            logger.warning("数据库连接已断开，尝试重连...")
            self._connect()

    def query(self, query, params=None, fetch_all=True):
        """执行SQL查询"""
        self._ensure_connected()
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
            # 如果是连接错误，尝试重连一次
            if "connection" in str(e).lower() or "closed" in str(e).lower():
                logger.warning(f"查询连接错误，尝试重连: {e}")
                self._ensure_connected()
                if self.is_connected():
                    try:
                        with self.conn.cursor() as cursor:
                            cursor.execute(query, params)
                            if fetch_all:
                                return cursor.fetchall()
                            return cursor.fetchone()
                    except Exception as retry_e:
                        logger.error(f"重连后查询失败: {retry_e}")
                        return None
            logger.error(f"查询执行失败: {e}\n查询: {query}\n参数: {params}")
            return None

    def update(self, query, params=None):
        """执行更新操作（INSERT/UPDATE/DELETE）"""
        self._ensure_connected()
        if not self.is_connected():
            logger.error("数据库未连接")
            return False

        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, params)
                return True
        except Exception as e:
            # 如果是连接错误，尝试重连一次
            if "connection" in str(e).lower() or "closed" in str(e).lower():
                logger.warning(f"更新连接错误，尝试重连: {e}")
                self._ensure_connected()
                if self.is_connected():
                    try:
                        with self.conn.cursor() as cursor:
                            cursor.execute(query, params)
                        return True
                    except Exception as retry_e:
                        logger.error(f"重连后更新失败: {retry_e}")
                        return False
            logger.error(f"更新操作失败: {e}\n查询: {query}\n参数: {params}")
            return False

    def create_table(self, query):
        """创建表"""
        self._ensure_connected()
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
        self._ensure_connected()
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

    def execute_raw_sql(self, query, params=None):
        """通过REST API执行原生SQL（支持UPDATE等操作）"""
        try:
            import requests
            from urllib.parse import quote

            config = self.config["questdb"]
            host = config["host"]
            port = 9000  # QuestDB REST API port

            # 构建查询URL (使用GET方法)
            full_query = query
            if params:
                for key, value in params.items():
                    if isinstance(value, str):
                        full_query = full_query.replace(f":{key}", f"'{value}'")
                    else:
                        full_query = full_query.replace(f":{key}", str(value))

            # URL编码查询
            encoded_query = quote(full_query, safe="")
            url = f"http://{host}:{port}/exec?query={encoded_query}"

            # Try without auth first
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                return True
            elif response.status_code == 401:
                # Try with basic auth
                from requests.auth import HTTPBasicAuth

                user = config.get("user", "admin")
                password = config.get("password", "")
                response = requests.get(
                    url, auth=HTTPBasicAuth(user, password), timeout=10
                )
                if response.status_code == 200:
                    return True
                else:
                    logger.error(
                        f"REST API auth failed: {response.status_code} - {response.text}"
                    )
                    return False
            else:
                logger.error(
                    f"REST API执行失败: {response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.error(f"REST API调用失败: {e}")
            return False

    @property
    def pd_conn(self):
        config_map = self.config["questdb"]
        return create_engine(
            f"""postgresql+psycopg2://{config_map["user"]}:{config_map["password"]}@{config_map["host"]}:{config_map["port"]}/{config_map["database"]}""",
            pool_pre_ping=True,  # 在使用连接前检查连接是否有效
            pool_recycle=1800,  # 每30分钟回收连接，避免空闲超时
            pool_size=5,  # 连接池大小
            max_overflow=10,  # 最大额外连接数
            pool_timeout=30,  # 获取连接超时时间
            pool_reset_on_return="rollback",  # 重置连接
        )

    @staticmethod
    def get_ohlc_table_name(freq):
        """根据股票代码和频率生成表名"""

        # 有效的频率后缀
        valid_freq = ["1min", "5min", "30min", "15min", "60min", "d", "w", "m"]

        # 检查频率是否有效
        if freq not in valid_freq:
            freq = "d"
            logger.info(f"警告: 未知频率 '{freq}', 使用默认频率 'd'")

        # 表名称映射
        tableName = {
            "1min": "ohlcv_1m",
            "5min": "ohlcv_5m",
            "15min": "ohlcv_15m",
            "30min": "ohlcv_30m",
            "60min": "ohlcv_60m",
            "d": "ohlcv_1d",
            "w": "ohlcv_1w",
            "m": "ohlcv_1mo",
        }

        return tableName[freq]


class BS:
    """Baostock API管理类，封装Baostock操作"""

    def __init__(self):
        self.connection = None
        self._login()

    def _login(self):
        """登录Baostock"""
        try:
            self.connection = baostock
            lg = self.connection.login()
            if lg.error_code == "0":
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
