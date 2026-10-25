"""BaoStock 连接封装。

BaoStock 是免费源，主要作为行情备用源、交易日历源和财务比率校验源。
它不提供完整三大报表，因此财务主源使用 Tushare/AkShare。

重要：``baostock`` 的会话是**模块级全局**的（``bs.login()`` / ``bs.logout()``
操作同一个 socket）。历史上每个 ``BaoStockClient`` 实例都登录一次、并在
``__del__`` 里 ``logout()``，导致：

- 多个实例（交易日历、行情源、指数源）互相替换/关闭 socket，出现
  ``WinError 10057``（socket 未连接）与「网络接收错误」；
- 临时对象被 GC 时把别人正在用的连接关掉。

因此这里改为**进程内共享一个会话**：所有调用方通过
``get_shared_baostock_client()`` 取同一个实例，且不再在 ``__del__`` 里登出。
"""

from __future__ import annotations

import atexit
import logging
import threading

from .exceptions import SourceUnavailableError

logger = logging.getLogger("DataInterface.BaoStock")

_shared_lock = threading.Lock()
_shared_client: "BaoStockClient | None" = None


class BaoStockClient:
    def __init__(self, auto_login: bool = True):
        self._module = None
        self._logged_in = False
        self._lock = threading.RLock()
        if auto_login:
            self.login()

    def login(self) -> bool:
        with self._lock:
            # 会话是全局的：已登录就直接复用，避免重复 login 把 socket 顶掉
            if self._logged_in and self._module is not None:
                return True
            try:
                import baostock as bs

                result = bs.login()
                if result.error_code != "0":
                    logger.error("BaoStock 登录失败: %s", result.error_msg)
                    return False
                self._module = bs
                self._logged_in = True
                return True
            except ImportError as exc:
                raise SourceUnavailableError("未安装 baostock，请执行 uv sync") from exc
            except Exception as exc:
                logger.error("BaoStock 登录异常: %s", exc)
                return False

    @property
    def client(self):
        if not self._logged_in or self._module is None:
            if not self.login():
                raise SourceUnavailableError("BaoStock 未登录")
        return self._module

    def is_logged_in(self) -> bool:
        return self._logged_in and self._module is not None

    def logout(self) -> None:
        with self._lock:
            if self._module is not None and self._logged_in:
                try:
                    self._module.logout()
                except Exception:
                    pass
                self._logged_in = False


def get_shared_baostock_client() -> BaoStockClient:
    """返回进程内共享的 ``BaoStockClient``（只登录一次）。"""
    global _shared_client
    with _shared_lock:
        if _shared_client is None:
            _shared_client = BaoStockClient()
        elif not _shared_client.is_logged_in():
            _shared_client.login()
        return _shared_client


def _logout_shared() -> None:
    global _shared_client
    if _shared_client is not None:
        _shared_client.logout()


# 进程退出时统一登出；不要用 __del__（会误关其它调用方的连接）
atexit.register(_logout_shared)
