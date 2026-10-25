from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger("DataInterface.TencentQuote")


class TencentQuoteSource:
    """腾讯单只股票实时行情。"""

    name = "tencent"

    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    def fetch_realtime(self, code: str) -> pd.DataFrame:
        import requests

        prefix = (
            "sh"
            if code.startswith(("6", "9", "5"))
            else ("bj" if code.startswith(("4", "8")) else "sz")
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
            close = float(data[3]) if data[3] else None
            if close is None:
                return pd.DataFrame()
            df = pd.DataFrame(
                [
                    {
                        "code": code,
                        "open": float(data[5]) if data[5] else close,
                        "high": float(data[33]) if data[33] else close,
                        "low": float(data[34]) if data[34] else close,
                        "close": close,
                        "volume": float(data[6]) * 100 if data[6] else 0.0,
                        "amount": float(data[37]) * 10000 if data[37] else 0.0,
                    }
                ]
            )
            df["ts"] = pd.Timestamp.now()
            df.set_index("ts", inplace=True)
            return df[["code", "open", "high", "low", "close", "volume", "amount"]]
        except Exception as exc:
            logger.warning("腾讯实时行情失败 %s: %s", code, exc)
            return pd.DataFrame()
