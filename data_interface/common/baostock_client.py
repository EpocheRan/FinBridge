"""BaoStock 连接封装。

BaoStock 是免费源，主要作为行情备用源、交易日历源和财务比率校验源。
它不提供完整三大报表，因此财务主源使用 Tushare/AkShare。
"""

from __future__ import annotations

import logging

from .exceptions import SourceUnavailableError

logger = logging.getLogger("DataInterface.BaoStock")


class BaoStockClient:
    def __init__(self, auto_login: bool = True):
        self._module = None
        self._logged_in = False
        if auto_login:
            self.login()

    def login(self) -> bool:
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
        if self._module is not None and self._logged_in:
            try:
                self._module.logout()
            except Exception:
                pass
            self._logged_in = False

    def __del__(self):
        self.logout()
