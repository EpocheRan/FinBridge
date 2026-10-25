"""QuestDB 访问客户端。

设计要点（针对 QuestDB）：

- QuestDB 没有传统主键/外键，幂等依赖 ``DEDUP UPSERT KEYS`` + WAL。
- 时序表必须有 designated timestamp，并按时间分区。
- 不在程序启动时建表；建表由 ``scripts`` 在主脚本/``docker db-init`` 中完成。
- 读取用 PostgreSQL wire protocol；DDL/少量 UPDATE 走 HTTP ``/exec``。
- numpy 适配器把 ``NaN/Inf`` 转成 ``NULL``，避免 ``nan`` 字面量写坏 LONG/INT 列。
- 连接探活改为「失败即重连」，不再在每次查询前多发一次 ``SELECT 1``。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import execute_values

from .config import get_config, resolve_questdb_host

logger = logging.getLogger("DataInterface.QuestDB")

# QuestDB 中常见的时间列，读取后统一转成 pandas datetime
_TS_COLUMNS = (
    "ts",
    "period_end",
    "as_of",
    "updated_at",
    "trade_date",
    "publish_date",
    "list_date",
    "delist_date",
    "found_date",
    "first_entry_date",
    "created_at",
)


def _quote_ident(name: str) -> str:
    """简单标识符校验，防止把用户输入当列名拼进 SQL。"""
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"非法标识符: {name!r}")
    return f'"{name}"'


def sql_literal(value: Any) -> str:
    """把 Python 值转成安全的 SQL 字面量（仅在无法参数化时使用）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "NULL"
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


class QuestDBClient:
    """QuestDB 连接与 SQL 执行封装。"""

    def __init__(self, config: dict[str, Any] | None = None, *, connect: bool = True):
        self.config = config or get_config()
        self._conn = None
        self._engine = None
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
        params = self._connection_params(host, port)
        try:
            self._conn = psycopg2.connect(**params)
            self._conn.autocommit = True
            logger.info("QuestDB connected: %s:%s", params["host"], params["port"])
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
            from psycopg2.extensions import AsIs, Float, Int, register_adapter

            def _adapt_float(value):
                # NaN/Inf 在 QuestDB 的 INT/LONG 列会报错，统一写 NULL
                return AsIs("NULL") if not math.isfinite(value) else Float(value)

            register_adapter(np.float64, _adapt_float)
            register_adapter(np.float32, _adapt_float)
            register_adapter(np.int64, Int)
            register_adapter(np.int32, Int)
        except Exception:
            pass
        self._adapters_registered = True

    def is_connected(self) -> bool:
        """只检查本地连接状态，不再每次发 ``SELECT 1``（省一个往返）。"""
        return self._conn is not None and self._conn.closed == 0

    def ping(self) -> bool:
        """显式探活（供健康检查使用）。"""
        if not self.is_connected():
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def ensure_connected(self) -> None:
        if not self.is_connected() and not self.connect():
            raise ConnectionError("QuestDB 未连接，请检查 config.json 与数据库状态")

    def _run(self, sql: str, params: Sequence[Any] | None = None, *, fetch: str = "none"):
        """执行 SQL；连接类异常自动重连并重试一次。"""
        for attempt in range(2):
            self.ensure_connected()
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch == "all":
                        return cur.fetchall()
                    if fetch == "one":
                        return cur.fetchone()
                    return None
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                logger.warning("QuestDB 连接异常（第 %s 次）: %s", attempt + 1, exc)
                self._conn = None
                if attempt == 1:
                    raise
        return None

    def close(self) -> None:
        if self._conn is not None and self._conn.closed == 0:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        if self._engine is not None:
            try:
                self._engine.dispose()
            except Exception:
                pass
            self._engine = None

    # ------------------------------------------------------------------ 查询
    def query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        fetch_all: bool = True,
    ):
        return self._run(sql, params, fetch="all" if fetch_all else "one")

    def query_dicts(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        self.ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def query_dataframe(self, sql: str, params: Sequence[Any] | None = None):
        """返回 pandas DataFrame，索引为 ``ts``（若存在）。

        直接用 psycopg2 游标构造 DataFrame：避免 SQLAlchemy 占位符重写，
        同时保证 ``%s`` 参数与 ``params`` 严格一一对应。
        """
        import pandas as pd

        self.ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
        frame = pd.DataFrame(rows, columns=columns)
        for column in _TS_COLUMNS:
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], errors="coerce")
        if "ts" in frame.columns:
            frame.set_index("ts", inplace=True)
        return frame

    @property
    def pd_conn(self):
        """SQLAlchemy engine（延迟创建、复用；供 position 模块的 ORM 使用）。"""
        if self._engine is not None:
            return self._engine
        from sqlalchemy import create_engine

        cfg = self.config["questdb"]
        url = (
            f"postgresql+psycopg2://{cfg.get('user', 'admin')}:{cfg.get('password', 'quest')}"
            f"@{resolve_questdb_host(self.config)}:{cfg['port']}/{cfg.get('database', 'stocks')}"
        )
        self._engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_reset_on_return="rollback",
        )
        return self._engine

    # ------------------------------------------------------------------ 写入
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> bool:
        try:
            self._run(sql, params)
            return True
        except Exception as exc:
            logger.error("QuestDB execute failed: %s | SQL=%s", exc, sql[:300])
            raise

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        for attempt in range(2):
            self.ensure_connected()
            try:
                with self._conn.cursor() as cur:
                    cur.executemany(sql, rows)
                return len(rows)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                self._conn = None
                if attempt == 1:
                    raise
                logger.warning("QuestDB executemany 连接异常，重试: %s", exc)
            except Exception as exc:
                logger.error("QuestDB executemany failed: %s | SQL=%s", exc, sql[:300])
                raise
        return 0

    def execute_values(self, sql_prefix: str, rows: Iterable[Sequence[Any]], page_size: int = 1000) -> int:
        """多行 VALUES 插入，适合中等批量写入（比逐行 executemany 少一个数量级往返）。"""
        rows = list(rows)
        if not rows:
            return 0
        for attempt in range(2):
            self.ensure_connected()
            try:
                with self._conn.cursor() as cur:
                    execute_values(cur, sql_prefix, rows, page_size=page_size)
                return len(rows)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                self._conn = None
                if attempt == 1:
                    raise
                logger.warning("QuestDB execute_values 连接异常，重试: %s", exc)
            except Exception as exc:
                logger.error("QuestDB execute_values failed: %s", exc)
                raise
        return 0

    def execute_ddl(self, sql: str) -> bool:
        """执行 DDL。QuestDB 的 DDL 走 PG wire 即可。"""
        return self.execute(sql)

    def execute_http(self, sql: str, timeout: int = 30) -> bool:
        """通过 HTTP ``/exec`` 执行 SQL（UPDATE/ALTER 等）。

        短 SQL 用 GET，长 SQL（如大 JSON）用 POST，避免 URL 长度限制。
        """
        import requests

        cfg = self.config["questdb"]
        url = f"http://{resolve_questdb_host(self.config)}:{cfg.get('rest_port', 9000)}/exec"
        try:
            if len(sql) < 1500:
                response = requests.get(url, params={"query": sql}, timeout=timeout)
            else:
                response = requests.post(url, data={"query": sql}, timeout=timeout)
            if response.status_code == 200:
                return True
            logger.error(
                "QuestDB HTTP exec failed: %s - %s | SQL=%s",
                response.status_code,
                response.text[:500],
                sql[:200],
            )
            return False
        except Exception as exc:
            logger.error("QuestDB HTTP exec exception: %s", exc)
            return False

    def execute_raw_sql(self, query: str, params: dict[str, Any] | None = None) -> bool:
        """兼容旧接口：把 ``:name`` 占位符替换成安全字面量后走 HTTP /exec。

        新代码请优先使用参数化的 :meth:`execute`；该方法仅为兼容保留。
        """
        full_query = query
        for key, value in (params or {}).items():
            full_query = full_query.replace(f":{key}", sql_literal(value))
        if ":" in full_query and params:
            # 占位符没被完全替换，宁可报错也不要执行半截 SQL
            logger.error("execute_raw_sql 存在未替换的占位符，已拒绝执行: %s", full_query[:200])
            return False
        return self.execute_http(full_query)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
