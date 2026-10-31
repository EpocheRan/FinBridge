"""A 股数据同步的数据集实现。

模块本身不负责定时，只提供可被 Agent 心跳反复调用的「有界批次」：

- 日线：按交易日批量拉取全市场（一次 Tushare daily 返回全市场），
  可选同时拉 ETF/LOF（``include_funds``）；
- 基准指数：补齐 Beta 所需的基准指数日线；
- 财务：按股票池分批，逐只拉取三大报表、派生指标与估值；
- Screener：按日期范围构建/修复日频因子快照；
- 两融/龙虎榜/大宗：按交易日批量拉取，写入情绪三表；
- 每次只处理有限数量，返回 next_cursor / next_cursors，下一次心跳从游标继续。

对外编排入口是 ``acquisition.orchestrator``（CLI ``python -m acquisition run``、
MCP ``acquisition_run``）；本类只提供各数据集的同步方法，不再承担总调度。

2026-10 修订：

- 日线写入 **不复权价 + adj_factor**（按交易日一次取全市场因子）；
- 某交易日拿不到数据时不再直接跳过：记 ``pending`` 并在后续心跳重试，
  超过上限才放弃并记 error，避免永久缺口；
- 支持 ``include_funds``（ETF/LOF 日线 + 基金基础信息）；
- 支持 ``codes`` 指定股票/ETF 代码（不传则全市场），
  指定代码时任务状态使用独立的 ``daily_codes`` / ``financial_codes`` 游标，
  不会覆盖全市场回填进度。
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import (
    is_a_share_code,
    normalize_code,
    ticker_type,
)
from data_interface.common.timeutil import market_closed_today, now_cn, today_str
from data_interface.common.trading_calendar import TradingCalendar
from data_interface.concept.repository import ConceptRepository
from data_interface.concept.service import ConceptService
from data_interface.concept.sources.em_source import EmConceptSource
from data_interface.concept.sources.ths_scrape_source import ThsScrapeConceptSource
from data_interface.concept.sources.ths_source import ThsConceptSource
from data_interface.concept.sources.tushare_classification import (
    TushareConceptClassificationSource,
)
from data_interface.financial.service import FinancialDataService
from data_interface.financial.sources.tushare_source import (
    TushareSource,
    get_shared_tushare_source,
)
from data_interface.market.adj_factors import AdjFactorProvider
from data_interface.market.fetcher import MarketDataFetcher
from data_interface.moneyflow.repository import MoneyflowRepository
from data_interface.moneyflow.service import MoneyflowService
from data_interface.moneyflow.sources.tushare_source import TushareMoneyflowSource

from ..state import TaskStateRepository

logger = logging.getLogger("Acquisition.AShareSync")

TASK_NAME = "a_share_sync"
STOP_ERRORS = (
    RateLimitExceededError,
    DailyQuotaExceededError,
    PermissionDeniedError,
    SourceUnavailableError,
)

# 全市场与指定代码使用不同状态键，避免互相覆盖游标。
DAILY_STATE = "daily"
DAILY_CODES_STATE = "daily_codes"
FINANCIAL_STATE = "financial"
FINANCIAL_CODES_STATE = "financial_codes"
BENCHMARK_STATE = "benchmark"
SCREENER_STATE = "screener"
MONEYFLOW_STATE = "moneyflow"
CONCEPT_META_STATE = "concept_meta"
CONCEPT_DAILY_STATE = "concept_daily"
CONCEPT_MEMBER_STATE = "concept_member"
CONCEPT_CLASS_STATE = "concept_class"
VALUATION_STATE = "valuation"


def normalize_selected_codes(
    codes: Sequence[str] | str | None,
) -> list[str] | None:
    """把用户传入的代码统一成排序后的 6 位代码列表。

    ``None``、空列表、空字符串都表示「不指定 = 全市场」。
    """
    if codes is None:
        return None
    if isinstance(codes, str):
        items = codes.replace(";", ",").split(",")
    else:
        items = [str(item) for item in codes]

    normalized: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        code = normalize_code(text)
        if len(code) != 6:
            logger.warning("忽略无法识别的证券代码: %s", text)
            continue
        normalized.add(code)
    return sorted(normalized) or None


def parse_date_range(
    start_date: str | None,
    end_date: str | None,
    *,
    default_start: str | None = None,
) -> tuple[date, date]:
    """解析并校验 YYYY-MM-DD 日期范围。"""
    end = date.fromisoformat(str(end_date)[:10]) if end_date else date.fromisoformat(today_str())
    if start_date:
        start = date.fromisoformat(str(start_date)[:10])
    elif default_start:
        start = date.fromisoformat(str(default_start)[:10])
    else:
        start = end
    if start > end:
        raise ValueError(f"start_date({start}) 不能晚于 end_date({end})")
    return start, end
