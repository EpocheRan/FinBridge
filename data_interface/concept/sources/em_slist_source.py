"""东方财富「个股 → 所属板块」适配器（一次请求拿全量归属）。

为什么用 ``slist/get`` 而不是板块列表接口
----------------------------------------
实测本机对 ``push2.eastmoney.com/api/qt/clist/get``（板块/成分列表）会被**直接断连**，
而 ``/api/qt/slist/get?spt=3`` 稳定可用，并且**一次请求**就能拿到某只股票的全部板块
归属（实测 603699 → 16 个），比「按板块逐个拉成分」少 3 个数量级请求。

接口语义（实测）
----------------
- ``spt=3``：该股票的所属板块列表，元素含 ``f12``（BK 板块代码）、``f14``（板块名）、
  ``f3``（当日涨幅 %）、``f62``（板块主力净额，元）。
- 返回顺序里**第 1 个通常是「所属行业」**（603699 首项为 ``BK1396 金属制品``），
  因此用 ``rank==0`` 标记 ``is_primary``；这是启发式，已在文档中标注。
- ``spt=1``（按板块取成分）返回 ``rc=102``，不可用；``spt=2`` 是「同行业个股」，
  可作行业簇的旁证但不是板块成分。

板块类型无法从响应字段判定（``f13`` 恒为 90），故用「名称规则 + 白名单」分类，
并把结果标成 ``board_type_source='heuristic'``，避免被误当成权威分类。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import requests

from data_interface.common.security_code import normalize_code, ticker_type as _ticker_type

logger = logging.getLogger("DataInterface.Concept.EmSlist")

_SLIST_URL = "https://push2.eastmoney.com/api/qt/slist/get"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

#: 非行业/非题材的属性类板块（指数成分、资金属性、风格、状态）
ATTRIBUTE_BOARDS = {
    "沪股通", "深股通", "融资融券", "转债标的", "标普道琼斯A股", "MSCI中国", "富时罗素",
    "上证50", "上证180", "上证380", "上证50_", "沪深300", "中证500", "中证1000", "中证100",
    "创业板综", "科创50", "北证50", "深证100R", "深成500",
    "私募重仓", "基金重仓", "基金增仓", "社保重仓", "QFII重仓", "保险重仓", "券商重仓",
    "信托重仓", "养老金", "证金持股", "汇金持股",
    "大盘股", "中盘股", "小盘股", "微盘股",
    "大盘成长", "大盘价值", "中盘成长", "中盘价值", "小盘成长", "小盘价值",
    "ST股", "次新股", "破净股", "高送转", "预盈预增", "预亏预减", "扭亏为盈",
    "股权激励", "股权转让", "举牌", "并购重组", "壳资源", "AB股", "AH股", "B股",
    "央企改革", "国企改革", "沪企改革", "深圳特区", "成渝特区", "长株潭",
}

#: 东方财富一级/二级行业板块（用于区分「行业」与「题材」）
INDUSTRY_BOARDS = {
    "金属制品", "机械设备", "通用设备", "专用设备", "仪器仪表", "电机", "电网设备",
    "风电设备", "光伏设备", "电池", "电源设备", "汽车零部件", "汽车整车", "商用车",
    "乘用车", "摩托车", "船舶制造", "航天航空", "军工电子", "半导体", "元件",
    "光学光电子", "消费电子", "电子化学品", "电子元件", "通信设备", "通信服务",
    "计算机设备", "软件开发", "IT服务", "互联网服务", "游戏", "文化传媒", "广告包装",
    "教育", "医疗服务", "化学制药", "生物制品", "医疗器械", "医药商业", "中药",
    "酿酒行业", "食品饮料", "农牧饲渔", "纺织服装", "家用轻工", "造纸印刷", "包装材料",
    "塑料制品", "橡胶制品", "化纤行业", "化学原料", "化学制品", "农药兽药", "化肥行业",
    "钢铁行业", "有色金属", "小金属", "贵金属", "能源金属", "煤炭行业", "石油行业",
    "燃气", "电力行业", "公用事业", "环保行业", "水务", "交运设备", "物流行业",
    "航空机场", "港口航运", "铁路公路", "房地产开发", "房地产服务", "工程建设",
    "装修装饰", "装修建材", "水泥建材", "玻璃玻纤", "银行", "保险", "证券", "多元金融",
    "商业百货", "贸易行业", "旅游酒店", "餐饮", "美容护理", "珠宝首饰", "家电行业",
    "白色家电", "黑色家电", "小家电", "厨卫电器", "综合行业", "专业服务",
    "工程咨询服务", "采掘行业", "石油加工", "非金属材料", "能源化工",
}


def classify_board(name: str) -> str:
    """按名称给板块定性：``industry`` / ``concept`` / ``region`` / ``index``。"""
    text = str(name or "").strip()
    if not text:
        return "concept"
    if text.endswith("板块"):
        return "region"
    if text in ATTRIBUTE_BOARDS:
        return "index"
    if text in INDUSTRY_BOARDS:
        return "industry"
    return "concept"


def _secid(code: str) -> str:
    code = normalize_code(code)
    if _ticker_type(code) in ("index",):
        return f"1.{code}" if code.startswith("000") else f"0.{code}"
    # 北交所：43/83/87/92 开头，东财 secid 的市场前缀也是 0
    if code.startswith(("43", "83", "87", "92")):
        return f"0.{code}"
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


class EmSlistBoardSource:
    """个股所属板块（东财 slist）。"""

    name = "eastmoney_slist"
    source_id = "em.slist"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        retries: int = 4,
        request_interval: float = 0.12,
        session: requests.Session | None = None,
    ):
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.request_interval = float(request_interval)
        self._session = session
        self._last_call = 0.0

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        return self._session

    # ------------------------------------------------------------------ 限速
    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        delta = time.monotonic() - self._last_call
        if delta < self.request_interval:
            time.sleep(self.request_interval - delta)
        self._last_call = time.monotonic()

    # ------------------------------------------------------------------ 取数
    def fetch_stock_boards(self, code: str) -> pd.DataFrame:
        """返回 ``rank/code/board_id/board_name/board_type/pct_chg/main_net``。"""
        code = normalize_code(code)
        if not code:
            raise ValueError("股票代码为空")
        params = {
            "spt": 3,
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f13,f14,f3,f62",
            "secid": _secid(code),
            "pn": 1,
            "pz": 60,
            "po": 1,
            "np": 1,
        }
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self.session.get(_SLIST_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
                last_error = exc
                logger.debug("slist 第 %s 次失败 %s: %s", attempt + 1, code, exc)
                time.sleep(0.4 * (attempt + 1))
        if payload is None:
            logger.warning("slist 取板块失败 %s: %s", code, last_error)
            return pd.DataFrame()
        data = (payload or {}).get("data") or {}
        items = data.get("diff") or []
        if not items:
            logger.warning("slist 返回空板块 %s: %s", code, str(payload)[:160])
            return pd.DataFrame()

        rows = []
        for rank, item in enumerate(items):
            board_id = str(item.get("f12") or "").strip()
            board_name = str(item.get("f14") or "").strip()
            if not board_id or not board_name:
                continue
            rows.append(
                {
                    "code": code,
                    "rank": rank,
                    "board_id": board_id,
                    "board_name": board_name,
                    "board_type": classify_board(board_name),
                    "pct_chg": item.get("f3"),
                    "main_net": item.get("f62"),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame.attrs["source"] = self.source_id
        return frame

    def fetch_industry_peers(self, code: str) -> list[str]:
        """``spt=2``：同行业个股（含自身）。

        用途：``spt=3`` 只能拿到「某只股票属于哪些板块」，要算**板块收益**还需要
        板块成分；``spt=1``（按板块取成分）实测返回 ``rc=102``，因此这里用
        「同行业个股」作为行业板块的成分近似（603699 实测 257 只，与东财
        「通用设备」板块 255 只基本一致）。
        """
        code = normalize_code(code)
        params = {
            "spt": 2,
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14",
            "secid": _secid(code),
            "pn": 1,
            "pz": 500,
            "po": 1,
            "np": 1,
        }
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self.session.get(_SLIST_URL, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                items = ((payload or {}).get("data") or {}).get("diff") or []
                codes = [normalize_code(str(item.get("f12"))) for item in items]
                return [item for item in dict.fromkeys(codes) if item]
            except Exception as exc:  # noqa: BLE001
                logger.debug("spt=2 第 %s 次失败 %s: %s", attempt + 1, code, exc)
                time.sleep(0.4 * (attempt + 1))
        logger.warning("spt=2 取同行业失败 %s", code)
        return []
