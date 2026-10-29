"""BaoStock 历史 K 线适配器。

- 只取**不复权**价（``adjustflag="3"``）；复权因子由 ``AdjFactorProvider`` 单独提供。
- ``turn`` 为换手率百分数，与项目统一口径一致。
- ``error_code != 0`` 必须暴露错误，不能当「无数据」。

熔断（实测教训）
----------------
本机对 BaoStock 的分钟查询会**长时间挂起**（>5 分钟无响应），一旦进了主流程就会
把整个同步任务卡死。这里加两层保护：

1. ``timeout_seconds``：查询放到守护线程里执行，超时即放弃（线程自行结束，不阻塞进程）；
2. 连续 ``max_consecutive_timeouts`` 次超时后**进程内熔断**，后续 ``available()``
   直接返回 ``False``，由上层切到其它源（如新浪）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, TypeVar

import pandas as pd

from data_interface.common.baostock_client import (
    BaoStockClient,
    get_shared_baostock_client,
)
from data_interface.common.security_code import normalize_code

logger = logging.getLogger("DataInterface.BaoStockMarket")

_T = TypeVar("_T")

_MINUTE_FREQ = {"5min": "5", "15min": "15", "30min": "30", "60min": "60"}
_DAILY_FREQ = {"d": "d", "w": "w", "m": "m"}


class BaoStockTimeout(RuntimeError):
    """BaoStock 查询超时。"""


def _call_with_timeout(func: Callable[[], _T], timeout: float) -> _T:
    """在守护线程中执行 ``func``，超过 ``timeout`` 秒抛 :class:`BaoStockTimeout`。"""
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回给调用方
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise BaoStockTimeout(f"BaoStock 查询超过 {timeout:.0f}s 未返回")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


class BaoStockMarketSource:
    """BaoStock 历史 K 线适配器。"""

    name = "baostock"

    def __init__(
        self,
        client: BaoStockClient | None = None,
        *,
        timeout_seconds: float = 20.0,
        max_consecutive_timeouts: int = 2,
    ):
        self._client = client
        self.timeout_seconds = float(timeout_seconds)
        self.max_consecutive_timeouts = max(1, int(max_consecutive_timeouts))
        self._consecutive_timeouts = 0
        self._disabled_reason: str | None = None

    @property
    def client(self) -> BaoStockClient:
        if self._client is None:
            self._client = get_shared_baostock_client()
        return self._client

    # ------------------------------------------------------------------ 熔断
    @property
    def disabled(self) -> bool:
        return self._disabled_reason is not None

    def reset_circuit(self) -> None:
        self._disabled_reason = None
        self._consecutive_timeouts = 0

    def _trip(self, reason: str) -> None:
        self._disabled_reason = reason
        logger.error("BaoStock 已熔断（%s），本次进程内不再尝试", reason)

    @staticmethod
    def _to_full_code(code: str) -> str:
        from data_interface.common.security_code import to_baostock_code

        return to_baostock_code(code)

    def available(self, freq: str) -> bool:
        """BaoStock 不支持 1 分钟线；熔断后一律不可用。"""
        if self.disabled:
            return False
        return freq != "1min"

    # ------------------------------------------------------------------ 取数
    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        code = normalize_code(stock_code)
        if freq not in _MINUTE_FREQ and freq not in _DAILY_FREQ:
            logger.warning("BaoStock 不支持频率 %s", freq)
            return pd.DataFrame()
        if self.disabled:
            logger.debug("BaoStock 已熔断，跳过 %s %s", code, freq)
            return pd.DataFrame()

        full_code = self._to_full_code(code)
        if freq.endswith("min"):
            fields = "date,time,open,high,low,close,volume,amount"
        else:
            fields = "date,open,high,low,close,volume,amount,turn"
        frequency = _MINUTE_FREQ.get(freq) or _DAILY_FREQ[freq]

        def query():
            return self.client.client.query_history_k_data_plus(
                full_code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                adjustflag="3",  # 3 = 不复权
            )

        try:
            result = _call_with_timeout(query, self.timeout_seconds)
        except BaoStockTimeout as exc:
            self._consecutive_timeouts += 1
            logger.warning("BaoStock 超时 %s %s: %s", code, freq, exc)
            if self._consecutive_timeouts >= self.max_consecutive_timeouts:
                self._trip(f"连续 {self._consecutive_timeouts} 次超时")
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001
            logger.warning("BaoStock 行情失败 %s: %s", code, exc)
            return pd.DataFrame()

        if result.error_code != "0":
            logger.warning("BaoStock 行情返回错误 %s: %s", code, result.error_msg)
            return pd.DataFrame()

        try:
            return _call_with_timeout(lambda: self._parse(result, code, freq), self.timeout_seconds)
        except BaoStockTimeout:
            self._consecutive_timeouts += 1
            logger.warning("BaoStock 遍历结果超时 %s %s", code, freq)
            if self._consecutive_timeouts >= self.max_consecutive_timeouts:
                self._trip("结果遍历超时")
            return pd.DataFrame()
        finally:
            self._consecutive_timeouts = 0

    @staticmethod
    def _parse(result, code: str, freq: str) -> pd.DataFrame:
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=result.fields)
        if freq.endswith("min"):
            frame["ts"] = pd.to_datetime(
                frame["time"].astype(str).str[:12], format="%Y%m%d%H%M", errors="coerce"
            )
            frame["code"] = code
            frame = frame.set_index("ts")
            out = frame[["code", "open", "high", "low", "close", "volume", "amount"]].copy()
            numeric = ["open", "high", "low", "close", "volume", "amount"]
        else:
            frame["ts"] = pd.to_datetime(frame["date"], errors="coerce")
            frame["code"] = code
            frame = frame.rename(columns={"turn": "turnover"})
            frame = frame.set_index("ts")
            out = frame[
                ["code", "open", "high", "low", "close", "volume", "amount", "turnover"]
            ].copy()
            numeric = ["open", "high", "low", "close", "volume", "amount", "turnover"]

        out[numeric] = out[numeric].apply(pd.to_numeric, errors="coerce")
        out = out.dropna(subset=["open", "high", "low", "close"]).sort_index()
        return out[~out.index.duplicated(keep="last")]
