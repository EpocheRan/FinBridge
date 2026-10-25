"""QuestDB 访问客户端。

设计要点（针对 QuestDB）：

- QuestDB 没有传统主键/外键，幂等依赖 ``DEDUP UPSERT KEYS`` + WAL。
- 时序表必须有 designated timestamp，并按时间分区。
- 不在程序启动时建表；建表由 ``scripts`` 在主脚本/``docker db-init`` 中完成。
- 读取用 PostgreSQL wire protocol；DDL/少量 UPDATE 走 HTTP ``/exec``。
- numpy/sqlalchemy 采用延迟导入，保证初始化脚本只依赖 psycopg2 也能运行。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import execute_values

from .config import get_config, resolve_questdb_host

logger = logging.getLogger("DataInterface.QuestDB")


class QuestDBClient:
    """QuestDB 连接与 SQL 执行封装。"""

    def __init__(self, config: dict[str, Any] | None = None, *, connect: bool = True):
        self.config = config or get_config()
        self._conn = None
        self._adapters_registered = False
        if connect:
            self.connect()

    # ------------------------------------------------------------------ 连接
    def _connection_params(self, host: str | None = None, port: int | None = None) -> dict:
        cfg = self.config["questdb"]
        return {
            "host": host or resolve_questdb_host(self.config),
            "port": int(port or cfg["port"]),
            "user": cfg.get("user", "admin"),
            "password": cfg.get("password", "quest"),
            "database": cfg.get("database", "stocks"),
            "connect_timeout": int(cfg.get("connect_timeout_seconds", 10)),
        }

    def connect(self, host: str | None = None, port: int | None = None) -> bool:
        self._register_adapters()
        try:
            self._conn = psycopg2.connect(**self._connection_params(host, port))
            self._conn.autocommit = True
            self._conn.set_session(autocommit=True, readonly=False)
            logger.info("QuestDB connected: %s:%s", self._connection_params(host, port)["host"], self._connection_params(host, port)["port"])
            return True
        except Exception as exc:
            logger.error("QuestDB connection failed: %s", exc)
            self._conn = None
            return False

    def _register_adapters(self) -> None:
        if self._adapters_registered:
            return
        try:
            import numpy as np
            from psycopg2.extensions import AsIs, register_adapter

            for dtype in (np.float64, np.float32, np.int64, np.int32):
                register_adapter(dtype, AsIs)
        except Exception:
            pass
        self._adapters_registered = True

    def is_connected(self) -> bool:
        try:
            if self._conn is None or self._conn.closed != 0:
                return False
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def ensure_connected(self) -> None:
        if not self.is_connected():
            if not self.connect():
                raise ConnectionError("QuestDB 未连接，请检查 config.json 与数据库状态")

    def close(self) -> None:
        if self._conn is not None and self._conn.closed == 0:
            self._conn.close()

    # ------------------------------------------------------------------ 查询
    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        fetch_all: bool = True,
    ):
        self.ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch_all else cur.fetchone()

    def query_dataframe(self, sql: str, params: Sequence[Any] | None = None):
        """返回 pandas DataFrame，索引为 ``ts``（若存在）。"""
        import pandas as pd
        from sqlalchemy import create_engine, text

        cfg = self.config["questdb"]
        url = (
            f"postgresql+psycopg2://{cfg.get('user', 'admin')}:{cfg.get('password', 'quest')}"
            f"@{resolve_questdb_host(self.config)}:{cfg['port']}/{cfg.get('database', 'stocks')}"
        )
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
        try:
            if params:
                # pandas + SQLAlchemy text() 使用 :name 占位符；这里把 %s 转成 :p0/:p1
                parts = sql.split("%s")
                named_sql = parts[0]
                named_params = {}
                for index, part in enumerate(parts[1:]):
                    key = f"p{index}"
                    named_sql += f":{key}" + part
                    named_params[key] = params[index]
                df = pd.read_sql(text(named_sql), engine, params=named_params)
            else:
                df = pd.read_sql(sql, engine)
        finally:
            engine.dispose()
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
            df.set_index("ts", inplace=True)
        return df

    @property
    def pd_conn(self):
        """SQLAlchemy engine（延迟创建，兼容 position 模块的 ORM 用法）。"""
        from sqlalchemy import create_engine

        cfg = self.config["questdb"]
        url = (
            f"postgresql+psycopg2://{cfg.get('user', 'admin')}:{cfg.get('password', 'quest')}"
            f"@{resolve_questdb_host(self.config)}:{cfg['port']}/{cfg.get('database', 'stocks')}"
        )
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_reset_on_return="rollback",
        )

    # ------------------------------------------------------------------ 写入
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> bool:
        self.ensure_connected()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
            return True
        except Exception as exc:
            logger.error("QuestDB execute failed: %s | SQL=%s", exc, sql[:200])
            raise

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        self.ensure_connected()
        rows = list(rows)
        if not rows:
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
            return len(rows)
        except Exception as exc:
            logger.error("QuestDB executemany failed: %s | SQL=%s", exc, sql[:200])
            raise

    def execute_values(self, sql_prefix: str, rows: Iterable[Sequence[Any]], page_size: int = 500) -> int:
        """多行 VALUES 插入，适合中等批量写入。"""
        self.ensure_connected()
        rows = list(rows)
        if not rows:
            return 0
        try:
            with self._conn.cursor() as cur:
                execute_values(cur, sql_prefix, rows, page_size=page_size)
            return len(rows)
        except Exception as exc:
            logger.error("QuestDB execute_values failed: %s", exc)
            raise

    def execute_ddl(self, sql: str) -> bool:
        """执行 DDL。QuestDB 的 DDL 走 PG wire 即可。"""
        return self.execute(sql)

    def execute_http(self, sql: str, timeout: int = 30) -> bool:
        """通过 HTTP ``/exec`` 执行 SQL，主要用于 UPDATE/ALTER 等。"""
        import requests

        cfg = self.config["questdb"]
        url = f"http://{resolve_questdb_host(self.config)}:{cfg.get('rest_port', 9000)}/exec"
        try:
            response = requests.get(url, params={"query": sql}, timeout=timeout)
            if response.status_code == 200:
                return True
            logger.error("QuestDB HTTP exec failed: %s - %s", response.status_code, response.text[:500])
            return False
        except Exception as exc:
            logger.error("QuestDB HTTP exec exception: %s", exc)
            return False

    def execute_raw_sql(self, query: str, params: dict[str, Any] | None = None) -> bool:
        """兼容旧接口：把 ``:name`` 占位符替换后走 HTTP /exec。

        QuestDB 的 UPDATE 能力有限，持仓模块仍使用该接口做原地更新。
        """
        full_query = query
        if params:
            for key, value in params.items():
                placeholder = f":{key}"
                if isinstance(value, str):
                    escaped = value.replace("'", "''")
                    full_query = full_query.replace(placeholder, f"'{escaped}'")
                elif value is None:
                    full_query = full_query.replace(placeholder, "NULL")
                else:
                    full_query = full_query.replace(placeholder, str(value))
        return self.execute_http(full_query)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
