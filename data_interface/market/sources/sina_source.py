"""新浪分钟线适配器（A 股分时兜底源）。

为什么需要它
------------
东财行情主机（``push2his`` / ``push2`` 的 kline 接口）从本机经常被直接断连，
baostock 的分钟查询实测会挂起数分钟，两条现成链路都不可用。新浪
``quotes.sina.cn`` 的 JSONP 接口稳定可用，因此作为**分时主力源**。

口径与限制（重要）
------------------
- 单次最多 **1023 根**（``datalen`` 上限，超过返回 ``null``），且**没有区间参数**，
  只能拿到「截至最新一根」的数据：
  - ``1min`` 约 **4 个交易日**；
  - ``5min`` 约 **21 个交易日**。
  因此本源的定位是「**当日增量 + 近端复核**」，不能替代历史全量回补。
- 没有 09:25 集合竞价的独立 bar；开盘集合竞价成交价等于当日首根 bar 的 ``open``，
  收盘集合竞价并入 ``15:00`` 那根 bar。
- 成交量单位已是「股」、成交额单位已是「元」（与库内口径一致），不再乘 100。
- 5min 响应缺少 ``amount`` 时补 0，并在 ``attrs`` 里标记。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pandas as pd
import requests

from data_interface.common.security_code import normalize_code

logger = logging.getLogger("DataInterface.SinaMarket")

_JSONP_URL = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/x/CN_MarketDataService.getKLineData"
    "?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}"
)
_JSONP_PATTERN = re.compile(r"x\((\[.*\])\)", re.S)

#: 新浪 ``scale`` 与库内频率的映射（``scale=240`` 是日线，本适配器不处理日线）
_SCALE_BY_FREQUENCY = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}
#: ``datalen`` 硬上限；超过新浪直接返回 ``null``（实测 1023 可用、2000 返回 null）
MAX_DATALEN = 1023
MINUTE_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "amount", "code")


def _sina_symbol(code: str) -> str:
    """``603699`` -> ``sh603699``；``000001`` -> ``sz000001``；``920662`` -> ``bj920662``。"""
    code = normalize_code(code)
    # 北交所：43/83/87/92 开头，必须先于「9 开头 = 沪市」判断
    if code.startswith(("43", "83", "87", "92")):
        return f"bj{code}"
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    if code.startswith(("0", "3", "1", "2")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    raise ValueError(f"无法识别的新浪证券代码: {code}")


def parse_jsonp(text: str) -> list[dict[str, Any]]:
    """解析新浪 JSONP 响应；空数据（``x(null)``）返回空列表。"""
    if not text:
        return []
    match = _JSONP_PATTERN.search(text)
    if not match:
        return []
    payload = match.group(1).strip()
    if payload in ("", "null", "[]"):
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("新浪 JSONP 解析失败: %s", text[:120])
        return []
    return data if isinstance(data, list) else []


class SinaMinuteSource:
    """新浪分钟线：只负责「取数 + 标准化」，落库由 ``MarketDataFetcher`` 完成。"""

    name = "sina"
    source_id = "sina"

    def __init__(
        self,
        timeout: float = 10.0,
        *,
        max_datalen: int = MAX_DATALEN,
        session: requests.Session | None = None,
        retries: int = 3,
    ):
        self.timeout = float(timeout)
        self.max_datalen = min(int(max_datalen), MAX_DATALEN)
        self.retries = int(retries)
        self._session = session

    # ------------------------------------------------------------------ 依赖
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Referer": "https://finance.sina.com.cn/",
                }
            )
        return self._session

    # ------------------------------------------------------------------ 能力
    @staticmethod
    def available(freq: str) -> bool:
        return freq in _SCALE_BY_FREQUENCY

    # ------------------------------------------------------------------ 取数
    def fetch_raw(self, stock_code: str, freq: str) -> list[dict[str, Any]]:
        if freq not in _SCALE_BY_FREQUENCY:
            raise ValueError(f"新浪分钟线不支持频率: {freq}")
        url = _JSONP_URL.format(
            symbol=_sina_symbol(stock_code),
            scale=_SCALE_BY_FREQUENCY[freq],
            datalen=self.max_datalen,
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return parse_jsonp(response.text)
            except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
                last_error = exc
                logger.debug("新浪分钟线第 %s 次失败 %s %s: %s", attempt + 1, stock_code, freq, exc)
        logger.warning("新浪分钟线失败 %s %s: %s", stock_code, freq, last_error)
        return []

    def fetch_ohlc(
        self, stock_code: str, start_date: str, end_date: str, freq: str
    ) -> pd.DataFrame:
        """返回 ``ts`` 索引、口径与 ``AkshareMarketSource`` 一致的 DataFrame。"""
        code = normalize_code(stock_code)
        rows = self.fetch_raw(code, freq)
        frame = self._standardize(rows, code)
        if frame.empty:
            return frame

        start_at = pd.Timestamp(f"{start_date} 00:00:00")
        end_at = pd.Timestamp(f"{end_date} 23:59:59")
        window = frame[(frame.index >= start_at) & (frame.index <= end_at)]
        window.attrs.update(frame.attrs)
        # 请求区间早于本源可覆盖范围时，明确告知调用方已截断
        if not frame.empty and start_at < frame.index[0]:
            window.attrs["truncated"] = True
            window.attrs["covered_from"] = frame.index[0]
            logger.warning(
                "新浪分钟线 %s %s 仅覆盖 %s 起（单次上限 %s 根），请求起点 %s 已被截断",
                code,
                freq,
                frame.index[0],
                self.max_datalen,
                start_at.date(),
            )
        return window

    @staticmethod
    def _standardize(rows: list[dict[str, Any]], code: str) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        rename = {"day": "ts", "date": "ts"}
        frame = frame.rename(columns={k: v for k, v in rename.items() if k in frame.columns})
        if "ts" not in frame.columns:
            return pd.DataFrame()
        missing_amount = "amount" not in frame.columns
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column not in frame.columns:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["amount"] = frame["amount"].fillna(0.0)
        frame["volume"] = frame["volume"].fillna(0)
        frame["code"] = code
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
        frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
        frame = frame.set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        result = frame[[column for column in MINUTE_COLUMNS if column != "ts"]]
        result.attrs["missing_amount"] = missing_amount
        result.attrs["source"] = "sina"
        return result
