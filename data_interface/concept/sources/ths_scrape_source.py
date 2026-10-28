"""同花顺概念成分股网页适配器（兜底源）。

AkShare 1.19.1 没有 ``stock_board_concept_cons_ths``，同花顺网页仍可抓：

- 第 1 页：``https://q.10jqka.com.cn/gn/detail/code/{code}/``
- 第 N 页：``https://q.10jqka.com.cn/gn/detail/field/264648/order/desc/page/{n}/ajax/1/code/{code}/``

单页 10 行，热门概念可到 35 页。反爬与结构都可能变化，因此：

- 只作为兜底源，低频调用；
- 每页失败重试 2 次，仍失败则停止并把 ``attrs['partial']=True``；
- 解析逻辑集中在 ``_parse_members``，出问题只改这一处。
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import pandas as pd
import requests

from ..models import SOURCE_THS_SCRAPE, Board

logger = logging.getLogger("DataInterface.Concept.ThsScrape")

_PAGE_URL = "https://q.10jqka.com.cn/gn/detail/code/{code}/"
_AJAX_URL = (
    "https://q.10jqka.com.cn/gn/detail/field/264648/order/desc/"
    "page/{page}/ajax/1/code/{code}/"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://q.10jqka.com.cn/gn/",
}


def _parse_members(html: str) -> pd.DataFrame:
    """从成分页 HTML 中提取 ``stock_code / stock_name``。"""
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return pd.DataFrame(columns=["stock_code", "stock_name"])
    if not tables:
        return pd.DataFrame(columns=["stock_code", "stock_name"])

    best: pd.DataFrame | None = None
    best_score = 0
    for table in tables:
        if table.empty:
            continue
        score = 0
        for column in table.columns:
            values = table[column].astype(str).str.strip()
            if values.str.fullmatch(r"\d{6}").mean() > 0.8:
                score += 2
        if "代码" in map(str, table.columns):
            score += 1
        if score > best_score:
            best, best_score = table, score
    if best is None:
        return pd.DataFrame(columns=["stock_code", "stock_name"])

    code_column = None
    if "代码" in best.columns:
        code_column = "代码"
    else:
        for column in best.columns:
            values = best[column].astype(str).str.strip()
            if values.str.fullmatch(r"\d{6}").mean() > 0.8:
                code_column = column
                break
    if code_column is None:
        return pd.DataFrame(columns=["stock_code", "stock_name"])

    name_column = None
    for candidate in ("名称", "股票名称", "证券名称"):
        if candidate in best.columns:
            name_column = candidate
            break
    if name_column is None:
        columns = list(best.columns)
        index = columns.index(code_column)
        name_column = columns[min(index + 1, len(columns) - 1)]

    frame = best[[code_column, name_column]].copy()
    frame.columns = ["stock_code", "stock_name"]
    frame["stock_code"] = frame["stock_code"].astype(str).str.strip()
    frame["stock_name"] = frame["stock_name"].astype(str).str.strip()
    frame = frame[frame["stock_code"].str.fullmatch(r"\d{6}")]
    return frame.drop_duplicates(subset=["stock_code"], keep="last").reset_index(drop=True)


class ThsScrapeConceptSource:
    source_id = SOURCE_THS_SCRAPE

    def __init__(
        self,
        *,
        max_pages: int = 60,
        sleep_seconds: float = 0.35,
        timeout: int = 15,
        retries: int = 2,
        session: Any | None = None,
    ):
        self.max_pages = max_pages
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout
        self.retries = retries
        self._session = session

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        return self._session

    def _get(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                # 同花顺概念页是 GBK
                response.encoding = "gbk"
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.sleep_seconds * (attempt + 1))
        raise RuntimeError(f"同花顺成分页请求失败: {url}: {last_error}") from last_error

    def fetch_members(self, board: Board) -> pd.DataFrame:
        code = str(board.source_code or board.board_code).strip()
        if not code.isdigit():
            raise ValueError(f"同花顺成分需要数字板块代码，收到: {code!r}")

        frames: list[pd.DataFrame] = []
        partial = False
        pages = 0
        for page in range(1, self.max_pages + 1):
            url = _PAGE_URL.format(code=code) if page == 1 else _AJAX_URL.format(page=page, code=code)
            try:
                html = self._get(url)
                frame = _parse_members(html)
            except Exception as exc:
                logger.warning("同花顺成分页失败 %s page=%s: %s", code, page, exc)
                partial = True
                break
            if frame.empty:
                break
            frames.append(frame)
            pages = page
            # 单页不足 10 行说明已经是最后一页（第 1 页也一样）
            if len(frame) < 10:
                break
            time.sleep(self.sleep_seconds)

        if not frames:
            result = pd.DataFrame(columns=["stock_code", "stock_name"])
        else:
            result = (
                pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset=["stock_code"], keep="last")
                .reset_index(drop=True)
            )
            # 到 max_pages 仍是满页，说明后面还有，标记为不完整
            if pages >= self.max_pages and len(frames[-1]) >= 10:
                partial = True
        result.attrs.update({"partial": partial, "pages": pages, "source": self.source_id})
        return result
