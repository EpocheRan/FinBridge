"""同花顺板块历史日线的快速通道（回补专用）。

AkShare 的 ``stock_board_concept_index_ths`` 每次调用都会先刷新一遍
「板块名称 -> 代码」映射（39 页），单板块约 13 秒；回补 375 个板块要 80 分钟。

这里直接复用同花顺的年度 K 线文件：

- ``https://q.10jqka.com.cn/gn/detail/code/{code}`` 取 ``clid``（板块内部代码）；
- ``https://d.10jqka.com.cn/v4/line/bk_{clid}/01/{year}.js`` 一年一个文件；
- ``v`` cookie 由 AkShare 自带的 ``ths.js`` 生成，进程内复用。

单板块约 1~2 秒，且解析逻辑与 AkShare 的结果列一致；仅用于回补脚本，
不改变线上 ``ThsConceptSource`` 的行为。
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

from ..models import SOURCE_THS, Board

logger = logging.getLogger("DataInterface.Concept.ThsFast")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


class ThsFastBoardSource:
    source_id = SOURCE_THS

    def __init__(self, *, session: Any | None = None, timeout: int = 15):
        self.timeout = timeout
        self._session = session
        self._v_code: str | None = None
        self._clid_cache: dict[str, str] = {}

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        return self._session

    def _v(self, refresh: bool = False) -> str:
        if self._v_code is None or refresh:
            import py_mini_racer
            from akshare.stock_feature.stock_board_concept_ths import (
                _get_file_content_ths,
            )

            js = py_mini_racer.MiniRacer()
            js.eval(_get_file_content_ths("ths.js"))
            self._v_code = js.call("v")
        return self._v_code

    def _clid(self, board_code: str) -> str:
        cached = self._clid_cache.get(board_code)
        if cached:
            return cached
        response = self.session.get(
            f"https://q.10jqka.com.cn/gn/detail/code/{board_code}",
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        soup = BeautifulSoup(response.text, "lxml")
        node = soup.find("input", attrs={"id": "clid"})
        if node is None or not node.get("value"):
            raise ValueError(f"板块 {board_code} 页面没有 clid")
        value = str(node["value"])
        self._clid_cache[board_code] = value
        return value

    def _year_frame(self, clid: str, year: int, *, v_code: str) -> pd.DataFrame:
        url = f"https://d.10jqka.com.cn/v4/line/bk_{clid}/01/{year}.js"
        headers = {
            "Referer": "http://q.10jqka.com.cn",
            "Host": "d.10jqka.com.cn",
            "Cookie": f"v={v_code}",
        }
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        if response.status_code == 404:
            # 该年份没有数据（板块尚未成立）
            return pd.DataFrame()
        response.raise_for_status()
        text = response.text
        start = text.find("{")
        if start < 0:
            return pd.DataFrame()
        from akshare.utils import demjson

        try:
            payload = demjson.decode(text[start:-1])
        except Exception:
            return pd.DataFrame()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            return pd.DataFrame()
        frame = pd.DataFrame(str(data).split(";"))
        frame = frame.iloc[:, 0].str.split(",", expand=True).iloc[:, :7]
        if frame.shape[1] < 7:
            return pd.DataFrame()
        frame.columns = ["date", "open", "high", "low", "close", "volume", "amount"]
        return frame

    def fetch_board_daily(
        self, board: Board, start_date: str | date, end_date: str | date
    ) -> pd.DataFrame:
        start = pd.Timestamp(start_date).normalize()
        end = pd.Timestamp(end_date).normalize()
        clid = self._clid(board.board_code)
        self._v()  # 预热 cookie 生成器

        frames: list[pd.DataFrame] = []
        for year in range(start.year, end.year + 1):
            frame = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    frame = self._year_frame(
                        clid, year, v_code=self._v(refresh=attempt > 0)
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(0.8 * (attempt + 1))
            if frame is None:
                logger.warning(
                    "同花顺年度文件失败 %s %s: %s", board.board_code, year, last_error
                )
                continue
            if not frame.empty:
                frames.append(frame)

        columns = ["ts", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        if not frames:
            return pd.DataFrame(columns=columns)
        frame = pd.concat(frames, ignore_index=True)
        frame["ts"] = pd.to_datetime(frame["date"], format="%Y%m%d", errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["ts", "close"]).sort_values("ts")
        frame = frame[(frame["ts"] >= start) & (frame["ts"] <= end)]
        frame = frame.drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
        frame["volume"] = frame["volume"].fillna(0).round().astype("int64")
        frame["pct_chg"] = frame["close"].pct_change()
        return frame[columns]
