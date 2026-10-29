"""东方财富「预约披露日 / 公告列表」适配器。

两个接口（实测均可用，且不依赖被断连的 ``push2his``）：

- 预约披露日：``datacenter-web.eastmoney.com/api/data/v1/get``
  ``reportName=RPT_PUBLIC_BS_APPOIN``，按 ``REPORT_DATE`` 分页，单期约 1117 页（pageSize=500）。
  返回 ``APPOINT_PUBLISH_DATE``（最新预约日）、``FIRST_APPOINT_DATE``（首次预约日）、
  三个改期字段、``ACTUAL_PUBLISH_DATE``（实际披露日，未披露为 null）、``RESIDUAL_DAYS``。
- 公告列表：``np-anotice-stock.eastmoney.com/api/security/ann``，按股票分页，
  返回 ``art_code / title / notice_date / columns[{column_code, column_name}]``。

Tushare 的 ``disclosure_date`` 在本账号（2120 分）返回 0 行，**不可用**，故只用东财。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import requests

from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

logger = logging.getLogger("DataInterface.Disclosure.EM")

_CALENDAR_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


class EmDisclosureSource:
    """预约披露日 + 公告列表。"""

    source_id = "em.disclosure"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        page_size: int = 500,
        max_pages: int = 1200,
        page_delay: float = 0.15,
        retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.timeout = float(timeout)
        self.page_size = max(1, int(page_size))
        self.max_pages = max(1, int(max_pages))
        self.page_delay = float(page_delay)
        self.retries = max(1, int(retries))
        self._session = session

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        return self._session

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                logger.debug("披露接口第 %s 次失败: %s", attempt + 1, exc)
                time.sleep(0.4 * (attempt + 1))
        logger.warning("披露接口请求失败: %s %s", url, params)
        return None

    # ------------------------------------------------------------------ 预约披露
    def fetch_calendar_page(self, report_date: str, page: int) -> tuple[list[dict], int]:
        params = {
            "reportName": "RPT_PUBLIC_BS_APPOIN",
            "columns": "ALL",
            "filter": f"(REPORT_DATE='{report_date}')",
            "pageNumber": page,
            "pageSize": self.page_size,
            "sortColumns": "APPOINT_PUBLISH_DATE",
            "sortTypes": 1,
            "source": "WEB",
            "client": "WEB",
        }
        payload = self._get_json(_CALENDAR_URL, params)
        result = (payload or {}).get("result") or {}
        return list(result.get("data") or []), int(result.get("pages") or 0)

    def fetch_calendar(self, report_date: str) -> pd.DataFrame:
        """拉取某一报告期（如 ``2026-09-30``）的全市场预约披露日历。"""
        rows: list[dict[str, Any]] = []
        pages = 0
        page = 1
        while page <= self.max_pages:
            data, total_pages = self.fetch_calendar_page(report_date, page)
            pages = total_pages or pages
            if not data:
                break
            rows.extend(data)
            if total_pages and page >= total_pages:
                break
            page += 1
            if self.page_delay:
                time.sleep(self.page_delay)
        frame = self._standardize_calendar(rows)
        frame.attrs["pages"] = pages or page - 1
        frame.attrs["report_date"] = report_date
        return frame

    def fetch_calendar_for_stock(self, code: str, limit: int = 8) -> pd.DataFrame:
        params = {
            "reportName": "RPT_PUBLIC_BS_APPOIN",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{normalize_code(code)}")',
            "pageNumber": 1,
            "pageSize": limit,
            "sortColumns": "REPORT_DATE",
            "sortTypes": -1,
            "source": "WEB",
            "client": "WEB",
        }
        payload = self._get_json(_CALENDAR_URL, params)
        rows = ((payload or {}).get("result") or {}).get("data") or []
        return self._standardize_calendar(list(rows))

    @staticmethod
    def _standardize_calendar(rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        rename = {
            "SECURITY_CODE": "code",
            "REPORT_DATE": "report_date",
            "REPORT_TYPE_NAME": "report_type",
            "APPOINT_PUBLISH_DATE": "appoint_date",
            "FIRST_APPOINT_DATE": "first_appoint_date",
            "FIRST_CHANGE_DATE": "first_change_date",
            "SECOND_CHANGE_DATE": "second_change_date",
            "THIRD_CHANGE_DATE": "third_change_date",
            "ACTUAL_PUBLISH_DATE": "actual_date",
            "RESIDUAL_DAYS": "residual_days",
            "IS_PUBLISH": "is_published_raw",
            "MARKET": "market",
        }
        frame = frame.rename(columns={k: v for k, v in rename.items() if k in frame.columns})
        for column in (
            "report_date",
            "appoint_date",
            "first_appoint_date",
            "first_change_date",
            "second_change_date",
            "third_change_date",
            "actual_date",
        ):
            if column not in frame.columns:
                frame[column] = pd.NaT
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
        if "residual_days" in frame.columns:
            frame["residual_days"] = pd.to_numeric(frame["residual_days"], errors="coerce")
        frame["is_published"] = (
            frame["is_published_raw"].astype(str).isin(["1", "True", "true"])
            if "is_published_raw" in frame.columns
            else frame["actual_date"].notna()
        )
        frame["code"] = frame["code"].astype(str).map(normalize_code)
        frame = frame[frame["code"].astype(str).str.len() == 6]
        return frame.reset_index(drop=True)

    # ------------------------------------------------------------------ 公告
    def fetch_announcements(
        self, code: str, *, page_size: int = 100, max_pages: int = 5, ann_type: str = "A"
    ) -> pd.DataFrame:
        code = normalize_code(code)
        rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params = {
                "sr": -1,
                "page_size": page_size,
                "page_index": page,
                "ann_type": ann_type,
                "client_source": "web",
                "stock_list": code,
                "f_node": 0,
                "s_node": 0,
            }
            payload = self._get_json(_ANNOUNCEMENT_URL, params)
            data = (payload or {}).get("data") or {}
            items = data.get("list") or []
            if not items:
                break
            rows.extend(items)
            total = int(data.get("total_hits") or 0)
            if total and page * page_size >= total:
                break
            if self.page_delay:
                time.sleep(self.page_delay)
        return self._standardize_announcements(rows)

    @staticmethod
    def _standardize_announcements(rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        records = []
        for item in rows:
            codes = item.get("codes") or []
            code = ""
            ann_type = ""
            if codes:
                code = normalize_code(str(codes[0].get("stock_code")))
                ann_type = str(codes[0].get("ann_type") or "")
            columns = item.get("columns") or [{}]
            records.append(
                {
                    "code": code,
                    "art_code": str(item.get("art_code") or ""),
                    "title": str(item.get("title") or "").strip(),
                    "ann_type": ann_type,
                    "column_code": str(columns[0].get("column_code") or ""),
                    "column_name": str(columns[0].get("column_name") or ""),
                    "notice_date": pd.to_datetime(item.get("notice_date"), errors="coerce"),
                    "display_time": pd.to_datetime(
                        str(item.get("display_time") or "")[:19], errors="coerce"
                    ),
                }
            )
        frame = pd.DataFrame(records)
        frame = frame[frame["code"].astype(str).str.len() == 6]
        return frame.reset_index(drop=True)

    @staticmethod
    def stamp(frame: pd.DataFrame) -> pd.DataFrame:
        """补 ``source`` / ``fetched_at`` 两列。"""
        if frame is None or frame.empty:
            return frame
        frame = frame.copy()
        frame["source"] = EmDisclosureSource.source_id
        frame["fetched_at"] = now_cn()
        return frame
