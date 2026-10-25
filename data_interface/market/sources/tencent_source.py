from __future__ import annotations

import logging

import pandas as pd

from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

logger = logging.getLogger("DataInterface.TencentQuote")


class TencentQuoteSource:
    """腾讯单只股票实时行情（不复权，最新价）。"""

    name = "tencent"

    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    def fetch_realtime(self, code: str) -> pd.DataFrame:
        import requests

        code = normalize_code(code)
        prefix = (
            "sh"
            if code.startswith(("6", "9", "5"))
            else ("bj" if code.startswith(("4", "8", "92")) else "sz")
        )
        try:
            response = requests.get(
                f"http://qt.gtimg.cn/q={prefix}{code}", timeout=self.timeout
            )
            response.encoding = "gbk"
            text = response.text
            if '="' not in text:
                return pd.DataFrame()
            data = text.split('="')[1].rstrip('";\n').split("~")
            if len(data) < 40:
                return pd.DataFrame()
            close = self._number(data[3])
            if close is None:
                return pd.DataFrame()
            df = pd.DataFrame(
                [
                    {
                        "code": code,
                        "open": self._number(data[5]) or close,
                        "high": self._number(data[33]) or close,
                        "low": self._number(data[34]) or close,
                        "close": close,
                        # 腾讯成交量单位「手」-> 股；成交额单位「万元」-> 元
                        "volume": (self._number(data[6]) or 0.0) * 100,
                        "amount": (self._number(data[37]) or 0.0) * 10000,
                    }
                ]
            )
            df["ts"] = pd.Timestamp(now_cn())
            return df.set_index("ts")[
                ["code", "open", "high", "low", "close", "volume", "amount"]
            ]
        except Exception as exc:
            logger.warning("腾讯实时行情失败 %s: %s", code, exc)
            return pd.DataFrame()

    @staticmethod
    def _number(value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
