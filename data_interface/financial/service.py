"""财务数据服务编排。

对外提供“按需同步 + 查询”能力：

- 基础信息/板块：Tushare ``stock_basic`` + AkShare 行业资料
- 三大报表：Tushare ``income/balancesheet/cashflow`` + ``fina_indicator``
- 每日估值：Tushare ``daily_basic`` + 本地 TTM/EV 计算
- 一致预期：Tushare ``report_rc``（每日 10 次）→ AkShare 盈利预测兜底

所有写操作先标准化，再按 QuestDB DEDUP 键写入；查询支持 ``as_of`` 时点。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from data_interface.common.config import get_config
from data_interface.common.trading_calendar import TradingCalendar
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    DataNotFoundError,
    PermissionDeniedError,
    RateLimitExceededError,
    SourceUnavailableError,
)
from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import (  # noqa: F401  (对外保持同名导出)
    board_of,
    exchange_of,
    is_a_share_code,
)
from data_interface.common.timeutil import now_cn

from .normalizer import (
    merge_fina_indicator,
    normalize_code,
    normalize_daily_basic,
    normalize_statement,
    parse_date,
    safe_float,
)
from .metrics import FinancialMetricsCalculator
from .repository import FinancialRepository
from .sources.akshare_source import AkShareFinancialSource
from .sources.tushare_source import TushareSource, get_shared_tushare_source

logger = logging.getLogger("DataInterface.FinancialService")


def _infer_exchange(code: str) -> str:
    return exchange_of(code)


def _infer_board(code: str) -> str:
    return board_of(code)


def _exchange_from_ts_code(ts_code: str) -> str:
    suffix = ts_code.split(".")[-1].upper() if "." in ts_code else ""
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, exchange_of(ts_code))


def _map_board(value: str | None, code: str) -> str:
    mapping = {
        "主板": "MAIN",
        "创业板": "GEM",
        "科创板": "STAR",
        "北交所": "BSE",
        "CDR": "CDR",
        "MAIN": "MAIN",
        "GEM": "GEM",
        "STAR": "STAR",
        "BSE": "BSE",
    }
    return mapping.get(str(value), _infer_board(code))


class FinancialDataService:
    def __init__(
        self,
        *,
        db: QuestDBClient | None = None,
        repository: FinancialRepository | None = None,
        tushare: TushareSource | None = None,
        akshare: AkShareFinancialSource | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        if db is not None:
            self.db = db
        elif repository is not None and getattr(repository, "db", None) is not None:
            self.db = repository.db
        else:
            self.db = QuestDBClient()
        self.repository = repository or FinancialRepository(self.db)
        self._tushare = tushare
        self.akshare = akshare or AkShareFinancialSource(self.config)
        self.metrics = FinancialMetricsCalculator()
        self.trading_calendar = TradingCalendar(self.db)

    @property
    def tushare(self) -> TushareSource:
        if self._tushare is None:
            self._tushare = get_shared_tushare_source(self.config)
        return self._tushare

    # ------------------------------------------------------------------ 基础信息
    def sync_profile(self, code: str) -> dict[str, Any]:
        code = normalize_code(code)
        now = now_cn()
        snapshot_ts = now.replace(hour=0, minute=0, second=0, microsecond=0)
        existing = self.repository.get_profile(code) or {}
        profile: dict[str, Any] = {
            "ts": snapshot_ts,
            "code": code,
            "exchange": _infer_exchange(code),
            "board": _infer_board(code),
            "market": "A",
            "security_type": "STOCK",
            "currency": "CNY",
            "list_status": existing.get("list_status") or "L",
            "source": "tushare+akshare",
            "updated_at": now,
        }

        tushare_error: Exception | None = None
        try:
            # 先查在市，再回退到暂停上市/退市；否则退市股永远拿不到
            # delist_date，sync_statements 的退市截断分支永远不可达。
            rows: list[dict[str, Any]] = []
            status_used: str | None = None
            for status in ("L", "P", "D"):
                rows = self.tushare.fetch_stock_basic(code=code, list_status=status)
                if rows:
                    status_used = status
                    break
            if rows:
                raw = rows[0]
                profile.update(
                    {
                        "name": raw.get("name"),
                        "fullname": raw.get("fullname"),
                        "exchange": raw.get("exchange") or profile["exchange"],
                        "board": _map_board(raw.get("market"), code),
                        "list_status": raw.get("list_status")
                        or status_used
                        or profile.get("list_status")
                        or "L",
                        "list_date": parse_date(raw.get("list_date")),
                        "delist_date": parse_date(raw.get("delist_date"))
                        or profile.get("delist_date"),
                        "industry": raw.get("industry"),
                        "area": raw.get("area"),
                    }
                )
        except Exception as exc:
            tushare_error = exc
            logger.warning("Tushare stock_basic 失败 %s: %s", code, exc)

        # 用最近一个交易日的 daily_basic 补齐股本/市值，避免依赖 AkShare 网页接口
        try:
            today = now_cn()
            rows = self.tushare.fetch_daily_basic(
                code,
                start_date=(today - timedelta(days=30)).strftime("%Y%m%d"),
                end_date=today.strftime("%Y%m%d"),
            )
            if rows:
                latest = sorted(rows, key=lambda item: str(item.get("trade_date")))[-1]
                total_share = safe_float(latest.get("total_share"))
                float_share = safe_float(latest.get("float_share"))
                total_mv = safe_float(latest.get("total_mv"))
                circ_mv = safe_float(latest.get("circ_mv"))
                profile.update(
                    {
                        "total_share": total_share * 10_000 if total_share is not None else profile.get("total_share"),
                        "float_share": float_share * 10_000 if float_share is not None else profile.get("float_share"),
                        "total_mv": total_mv * 10_000 if total_mv is not None else profile.get("total_mv"),
                        "float_mv": circ_mv * 10_000 if circ_mv is not None else profile.get("float_mv"),
                    }
                )
        except Exception as exc:
            logger.debug("daily_basic 补充股本/市值失败 %s: %s", code, exc)

        try:
            ak_profile = self.akshare.fetch_profile(code)
            if ak_profile:
                for key, value in ak_profile.items():
                    if value is not None and not profile.get(key):
                        profile[key] = value
                if ak_profile.get("industry") and not profile.get("industry"):
                    profile["industry"] = ak_profile["industry"]
                profile["source"] = "akshare" if tushare_error else "tushare+akshare"
        except Exception as exc:
            logger.warning("AkShare 基础信息失败 %s: %s", code, exc)

        # 本次没拿到的字段保留库里的旧值；否则 Tushare 某次请求失败
        # 或接口少字段时，会用 NULL 覆盖已经完整的 profile。
        for key, value in existing.items():
            if key in ("ts", "code", "updated_at"):
                continue
            if profile.get(key) in (None, "") and value is not None:
                profile[key] = value
        profile["list_status"] = profile.get("list_status") or "L"

        name = profile.get("name") or ""
        profile["is_st"] = "ST" in name.upper() or name.startswith("*ST")

        if not profile.get("name"):
            if tushare_error:
                raise tushare_error
            raise DataNotFoundError(f"未找到 {code} 的基础信息", source="financial")

        self.repository.upsert_profile([profile])

        # 主行业先落一条板块归属；概念/指数等由 sync_board_catalog 批量补充
        industry = profile.get("industry")
        if industry:
            self.repository.upsert_stock_boards(
                [
                    {
                        "ts": snapshot_ts,
                        "code": code,
                        "board_id": f"industry:{industry}",
                        "board_type": "industry",
                        "board_name": industry,
                        "is_primary": True,
                        "effective_from": now,
                        "effective_to": None,
                        "source": profile.get("source", "tushare"),
                    }
                ]
            )

        self._write_sync_state(code, "profile", status="OK")
        return self.repository.get_profile(code) or profile

    # ------------------------------------------------------------------ 股票池
    def sync_stock_universe(self, *, include_delisted: bool = True) -> int:
        """拉取 A 股列表并写入 ``stock_basic_info``。

        ``include_delisted=True`` 时会额外拉取 ``D/P`` 状态，目的是把
        ``delist_date/list_status`` 真正落库；股票池查询仍只返回 ``L``。
        这样退市股不会被当成在市股票反复抓取，``sync_statements`` 的
        ``delist_date`` 截断也才有意义。
        """
        statuses = ("L", "P", "D") if include_delisted else ("L",)
        rows: list[tuple[str, dict[str, Any]]] = []
        for status in statuses:
            try:
                batch = self.tushare.fetch_stock_basic(list_status=status)
            except Exception as exc:
                if status == "L":
                    raise
                logger.warning("拉取 %s 状态股票失败: %s", status, exc)
                continue
            rows.extend((status, raw) for raw in batch)

        now = now_cn()
        snapshot_ts = now.replace(hour=0, minute=0, second=0, microsecond=0)
        profiles = []
        for fallback_status, raw in rows:
            ts_code = str(raw.get("ts_code") or "")
            code = normalize_code(ts_code)
            if not code or not is_a_share_code(code):
                continue
            name = raw.get("name") or ""
            profiles.append(
                {
                    "ts": snapshot_ts,
                    "code": code,
                    "name": name,
                    "fullname": raw.get("fullname"),
                    "exchange": _exchange_from_ts_code(ts_code),
                    "board": _map_board(raw.get("market"), code),
                    "market": "A",
                    "security_type": "STOCK",
                    "currency": "CNY",
                    "is_st": "ST" in name.upper() or name.startswith("*ST"),
                    "list_status": raw.get("list_status") or fallback_status,
                    "list_date": parse_date(raw.get("list_date")),
                    "delist_date": parse_date(raw.get("delist_date")),
                    "industry": raw.get("industry"),
                    "area": raw.get("area"),
                    "source": "tushare",
                    "updated_at": now,
                }
            )
        if profiles:
            self.repository.upsert_profile(profiles)
        return len(profiles)

    def get_stock_universe(self) -> list[str]:
        return [
            code
            for code in self.repository.get_profile_codes()
            if is_a_share_code(code)
        ]

    def ensure_stock_universe(self, min_count: int = 1000) -> list[str]:
        codes = self.get_stock_universe()
        if len(codes) < min_count:
            self.sync_stock_universe()
            codes = self.get_stock_universe()
        return codes

    # ------------------------------------------------------------------ 板块
    def sync_board_catalog(self, board_types: tuple[str, ...] = ("industry", "concept")) -> int:
        """同步东财行业/概念板块目录。"""
        total = 0
        now = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        for board_type in board_types:
            catalog = self.akshare.fetch_board_catalog(board_type)
            rows = [
                {
                    "ts": now,
                    "board_id": item["board_id"],
                    "board_type": item["board_type"],
                    "board_name": item["board_name"],
                    "parent_id": item.get("parent_id"),
                    "source": item.get("source", "akshare"),
                }
                for item in catalog
            ]
            total += self.repository.upsert_boards(rows)
        return total

    def sync_board_members(self, board_name: str, board_type: str = "industry") -> int:
        """同步单个板块的成分股。"""
        board_id = board_name
        try:
            catalog = self.akshare.fetch_board_catalog(board_type)
            mapping = {item["board_name"]: item["board_id"] for item in catalog}
            board_id = mapping.get(board_name, board_name)
        except Exception:
            pass
        members = self.akshare.fetch_board_members(board_name, board_type)
        now = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = [
            {
                "ts": now,
                "code": item["code"],
                "board_id": board_id,
                "board_type": item.get("board_type", f"em_{board_type}"),
                "board_name": board_name,
                "is_primary": board_type == "industry",
                "effective_from": now,
                "effective_to": None,
                "source": "akshare",
            }
            for item in members
        ]
        return self.repository.upsert_stock_boards(rows)

    # ------------------------------------------------------------------ 增量判断
    #: 法定披露截止日（报告期 -> 最晚披露月/日偏移），未到截止日不视为缺失
    DEFAULT_DISCLOSURE_LAGS = {"Q1": 30, "H1": 62, "Q3": 31, "FY": 120}

    def _disclosure_lags(self) -> dict[str, int]:
        configured = self.config["financial"].get("disclosure_lag_days")
        lags = dict(self.DEFAULT_DISCLOSURE_LAGS)
        if isinstance(configured, dict):
            for key, value in configured.items():
                if key in lags:
                    try:
                        lags[key] = int(value)
                    except (TypeError, ValueError):
                        continue
        elif configured is not None:
            # 兼容旧的单一 report_lag_days：仅作为 FY 的滞后
            try:
                lags["FY"] = int(configured)
            except (TypeError, ValueError):
                pass
        return lags

    def _expected_statement_periods(
        self, start_year: int, end_year: int
    ) -> list[datetime]:
        """按季度生成应该存在的报告期。

        不同报告期用不同的披露滞后（Q1 30 天、H1 62 天、Q3 31 天、FY 120 天），
        避免 2 月/8 月把「还没到披露期」误判成缺失后反复全量抓取。
        """
        lags = self._disclosure_lags()
        period_lags = ((3, 31, lags["Q1"]), (6, 30, lags["H1"]), (9, 30, lags["Q3"]), (12, 31, lags["FY"]))
        now = now_cn()
        periods = []
        for year in range(start_year, end_year + 1):
            for month, day, lag in period_lags:
                period = datetime(year, month, day)
                if period + timedelta(days=lag) <= now:
                    periods.append(period)
        return periods

    def _missing_statement_periods(
        self,
        code: str,
        statement_type: str,
        start_year: int,
        end_year: int,
        *,
        report_type: str = "consolidated",
    ) -> list[datetime]:
        expected = set(self._expected_statement_periods(start_year, end_year))
        actual = self.repository.get_statement_periods(
            code, statement_type, report_type=report_type
        )
        return sorted(expected - actual)

    def _trading_days(self, start_date: str, end_date: str) -> list[str]:
        """交易日；日历不可用/未覆盖时回退工作日（否则估值同步会静默跳过）。"""
        return self.trading_calendar.trading_days_or_weekdays(start_date, end_date)

    def _missing_valuation_dates(
        self, code: str, start_date: str, end_date: str
    ) -> list[date]:
        expected = {
            date.fromisoformat(day)
            for day in self._trading_days(start_date, end_date)
        }
        actual = self.repository.get_valuation_dates(
            code, start_date=start_date, end_date=end_date
        )
        return sorted(expected - actual)

    # ------------------------------------------------------------------ 三大报表
    def sync_statements(
        self,
        code: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        force: bool = False,
        report_type: str = "consolidated",
    ) -> dict[str, int]:
        code = normalize_code(code)
        start_year = start_year or int(
            self.config["financial"].get("statement_start_year", 2000)
        )
        end_year = end_year or now_cn().year

        # 未上市/已退市年份不应被视为“缺失报告期”
        profile = self.repository.get_profile(code)
        if profile:
            list_date = profile.get("list_date")
            delist_date = profile.get("delist_date")
            if isinstance(list_date, datetime):
                start_year = max(start_year, list_date.year)
            if isinstance(delist_date, datetime):
                end_year = min(end_year, delist_date.year)
        if end_year < start_year:
            return {"IS": 0, "BS": 0, "CF": 0}

        missing = {
            statement_type: self._missing_statement_periods(
                code, statement_type, start_year, end_year, report_type=report_type
            )
            for statement_type in ("IS", "BS", "CF")
        }
        has_missing = any(missing.values())

        # 数据库已覆盖全部预期报告期，且不是强制刷新：不再请求第三方
        if not force and not has_missing:
            return {"IS": 0, "BS": 0, "CF": 0}

        lookback_years = int(
            self.config["financial"].get("restatement_lookback_years", 1)
        )
        if has_missing:
            fetch_start_year = min(
                period.year for periods in missing.values() for period in periods
            )
        else:
            # 强制刷新只回看最近 N 年，用于捕获财报重述。
            # 锚点必须是**请求的 end_year**，不能用当前年：否则补 2024 年数据时
            # 会算出 2025~2024 这种空区间，第三方返回空表后被当成同步失败。
            anchor_year = min(end_year, now_cn().year)
            fetch_start_year = max(start_year, anchor_year - lookback_years)
        fetch_start_year = min(fetch_start_year, end_year)

        start_date = f"{fetch_start_year}0101"
        end_date = f"{end_year}1231"
        report_type_param = {"consolidated": "1", "single_quarter": "2"}.get(
            report_type, "1"
        )

        raw_income = self.tushare.fetch_income(
            code,
            start_date=start_date,
            end_date=end_date,
            report_type=report_type_param,
        )
        raw_balance = self.tushare.fetch_balance_sheet(
            code, start_date=start_date, end_date=end_date, report_type=report_type_param
        )
        raw_cash = self.tushare.fetch_cash_flow(
            code, start_date=start_date, end_date=end_date, report_type=report_type_param
        )
        raw_fina = self.tushare.fetch_fina_indicator(
            code, start_date=start_date, end_date=end_date
        )
        if not (raw_income or raw_balance or raw_cash):
            raise DataNotFoundError(
                f"{code} 三大报表均无数据（ts_code={code}，{start_date}~{end_date}）",
                source="tushare",
            )

        now = now_cn()
        statements: dict[str, list[dict[str, Any]]] = {"IS": [], "BS": [], "CF": []}
        for statement_type, raw_rows in (
            ("IS", raw_income),
            ("BS", raw_balance),
            ("CF", raw_cash),
        ):
            for raw in raw_rows:
                row = normalize_statement(
                    statement_type, raw, source="tushare", ingested_at=now
                )
                if row:
                    statements[statement_type].append(row)

        if raw_fina:
            merge_fina_indicator(statements["IS"], raw_fina)
            merge_fina_indicator(statements["BS"], raw_fina)
            merge_fina_indicator(statements["CF"], raw_fina)

        counts = {}
        for statement_type, rows in statements.items():
            counts[statement_type] = self.repository.upsert_statements(
                statement_type, rows
            )
            self.repository.upsert_report_meta(rows)
            self._write_raw_reports(statement_type, rows, now)

        # 指标必须用**全量历史**重算：只用本次抓取窗口会把已有同比覆盖成 None
        self.sync_indicators(code)

        latest = max(
            (row["period_end"] for rows in statements.values() for row in rows),
            default=None,
        )
        self._write_sync_state(
            code,
            "statements",
            status="OK" if latest else "EMPTY",
            last_period_end=latest,
        )
        return counts

    # ------------------------------------------------------------------ 估值
    def sync_valuation(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        force: bool = False,
    ) -> int:
        code = normalize_code(code)
        end_date = end_date or now_cn().strftime("%Y-%m-%d")
        start_date = start_date or (
            f"{self.config['financial'].get('valuation_start_year', 2020)}-01-01"
        )

        # 数据库已覆盖区间内所有交易日：不再请求第三方
        if not force:
            missing_dates = self._missing_valuation_dates(code, start_date, end_date)
            if not missing_dates:
                return 0

        try:
            daily_rows = self.tushare.fetch_daily_basic(
                code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except (DailyQuotaExceededError, PermissionDeniedError, RateLimitExceededError, SourceUnavailableError) as exc:
            # 配置里声明了 akshare_indicator 兜底，这里真正接线（此前只是空承诺）
            logger.warning("Tushare daily_basic 不可用，降级 AkShare 估值: %s", exc)
            return self._try_akshare_valuation(code, start_date, end_date, str(exc))

        if not daily_rows:
            # Tushare 返回空也可能只是该标的不在 daily_basic 覆盖范围；
            # 既然配置声明了第二优先级，空结果也应尝试 AkShare。
            return self._try_akshare_valuation(
                code, start_date, end_date, "daily_basic 无数据"
            )

        # PIT 需要全部版本：先按 as_of 选「当时已知的最新一版」
        income_rows = self.repository.get_statements(code, "IS", latest_per_period=False)
        balance_rows = self.repository.get_statements(code, "BS", latest_per_period=False)
        cash_rows = self.repository.get_statements(code, "CF", latest_per_period=False)
        if not income_rows:
            self.sync_statements(code)
            income_rows = self.repository.get_statements(code, "IS", latest_per_period=False)
            balance_rows = self.repository.get_statements(code, "BS", latest_per_period=False)
            cash_rows = self.repository.get_statements(code, "CF", latest_per_period=False)

        now = now_cn()
        output = []
        previous_market_cap = None
        for raw in sorted(daily_rows, key=lambda item: item.get("trade_date", "")):
            basic = normalize_daily_basic(raw, ingested_at=now)
            if basic is None:
                continue
            as_of = basic["ts"]
            visible_income = self._visible_as_of(income_rows, as_of)
            visible_balance = self._visible_as_of(balance_rows, as_of)
            visible_cash = self._visible_as_of(cash_rows, as_of)
            row = self.metrics.build_valuation_row(
                code=code,
                trade_date=as_of,
                daily_basic=basic,
                income_rows=visible_income,
                balance_rows=visible_balance,
                cash_flow_rows=visible_cash,
                previous_market_cap=previous_market_cap,
            )
            output.append(row)
            previous_market_cap = row.get("market_cap")

        count = self.repository.upsert_valuation(output)
        self._wait_for_valuation_visibility(code, start_date, end_date, len(output))
        self._write_sync_state(
            code,
            "valuation",
            status="OK" if output else "EMPTY",
            last_period_end=output[-1]["ts"] if output else None,
        )
        return count

    def sync_valuation_by_date(
        self,
        trade_date: str | date,
        *,
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """按交易日批量补齐全市场估值（``daily_basic`` 全市场 + 报表 PIT）。

        与逐只 ``sync_valuation`` 的区别：一次 Tushare ``daily_basic`` 取全市场，
        三张报表用 ``get_statements_bulk`` 一次载入后按 code 分组做 PIT，
        避免 5000 次单票请求。估值倍数口径与逐只路径完全一致
        （``FinancialMetricsCalculator.build_valuation_row``）。
        """
        day = str(trade_date)[:10]
        as_of = datetime.fromisoformat(day).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        raw_rows = self.tushare.fetch_daily_basic_all(day.replace("-", ""))
        if not raw_rows:
            return {
                "dataset": "valuation",
                "date": day,
                "rows_written": 0,
                "skipped": 0,
                "reason": "daily_basic 无数据（未收盘或非交易日）",
                "done": True,
            }

        income_by_code = self._statements_by_code("IS", as_of)
        balance_by_code = self._statements_by_code("BS", as_of)
        cash_by_code = self._statements_by_code("CF", as_of)

        wanted = (
            {normalize_code(str(code)) for code in codes} if codes else None
        )
        now = now_cn()
        output: list[dict[str, Any]] = []
        skipped = 0
        for raw in raw_rows:
            code = normalize_code(str(raw.get("ts_code") or ""))
            if not code or not is_a_share_code(code):
                skipped += 1
                continue
            if wanted is not None and code not in wanted:
                skipped += 1
                continue
            basic = normalize_daily_basic(raw, ingested_at=now)
            if basic is None:
                skipped += 1
                continue
            row = self.metrics.build_valuation_row(
                code=code,
                trade_date=basic["ts"],
                daily_basic=basic,
                income_rows=self._visible_as_of(
                    income_by_code.get(code, []), basic["ts"]
                ),
                balance_rows=self._visible_as_of(
                    balance_by_code.get(code, []), basic["ts"]
                ),
                cash_flow_rows=self._visible_as_of(
                    cash_by_code.get(code, []), basic["ts"]
                ),
            )
            output.append(row)

        written = self.repository.upsert_valuation(output) if output else 0
        return {
            "dataset": "valuation",
            "date": day,
            "rows_written": written,
            "skipped": skipped,
            "done": True,
        }

    def _statements_by_code(
        self, statement_type: str, as_of: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.repository.get_statements_bulk(statement_type, as_of):
            grouped.setdefault(str(row.get("code")), []).append(row)
        return grouped

    def _try_akshare_valuation(
        self, code: str, start_date: str, end_date: str, reason: str
    ) -> int:
        try:
            fallback_rows = self.akshare.fetch_valuation(
                code, start_date=start_date, end_date=end_date
            )
        except Exception as exc:
            logger.warning("AkShare 估值兜底失败 %s: %s", code, exc)
            fallback_rows = []
        if not fallback_rows:
            self._write_sync_state(
                code, "valuation", status="EMPTY", message=(reason or "估值无数据")[:500]
            )
            return 0
        count = self.repository.upsert_valuation(fallback_rows)
        self._write_sync_state(
            code, "valuation", status="PARTIAL", message=(reason or "AkShare 估值兜底")[:500]
        )
        return count

    def _wait_for_valuation_visibility(
        self, code: str, start_date: str, end_date: str, expected: int, timeout: float = 10.0
    ) -> None:
        """QuestDB WAL 异步落盘，写入后轮询等待可读，避免 MCP 立即查询漏行。"""
        if expected <= 0:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.db.query(
                'SELECT count() FROM "fin_valuation_daily" '
                "WHERE code = %s AND ts BETWEEN CAST(%s AS TIMESTAMP) AND CAST(%s AS TIMESTAMP)",
                (code, start_date, end_date),
                fetch_all=False,
            )
            if row and row[0] >= expected:
                return
            time.sleep(0.2)
        logger.warning(
            "估值写入后等待可读超时: code=%s range=%s~%s expected=%s",
            code,
            start_date,
            end_date,
            expected,
        )

    @staticmethod
    def _visible_as_of(rows: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
        """Point-in-Time：每个报告期只保留 ``publish_date <= as_of`` 的**最新版本**。

        旧实现先把同一报告期压成「最新披露版本」再过滤，一旦发生财报重述，
        重述日之前的历史估值就会整体丢掉该季度。
        """
        visible: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            publish_date = row.get("publish_date")
            # 没有披露日的数据无法做时点判断，估值计算中直接排除，避免前视偏差
            if publish_date is None or publish_date > as_of:
                continue
            key = (row.get("period_end"), row.get("report_type"))
            current = visible.get(key)
            if current is None or publish_date > current.get("publish_date"):
                visible[key] = row
        return sorted(visible.values(), key=lambda item: item["period_end"])

    # ------------------------------------------------------------------ 一致预期
    def sync_estimates(self, code: str) -> dict[str, Any]:
        code = normalize_code(code)
        warnings: list[str] = []
        rows: list[dict[str, Any]] = []

        try:
            raw = self.tushare.fetch_report_rc(code)
            rows.extend(self._normalize_report_rc(code, raw))
        except (DailyQuotaExceededError, PermissionDeniedError, RateLimitExceededError) as exc:
            warnings.append(str(exc))
            logger.warning("Tushare report_rc 不可用，降级 AkShare: %s", exc)
        except Exception as exc:
            warnings.append(str(exc))
            logger.warning("Tushare report_rc 失败，降级 AkShare: %s", exc)

        # Tushare report_rc 有时只返回 EPS/PE；用 AkShare 补齐收入/净利润预期
        has_fundamental_estimate = any(
            row.get("metric") in ("revenue", "net_income") for row in rows
        )
        if not rows or not has_fundamental_estimate:
            try:
                rows.extend(self.akshare.fetch_estimates(code))
            except Exception as exc:
                warnings.append(f"AkShare 盈利预测失败: {exc}")

        if rows:
            self.repository.upsert_estimates(rows)

        self._write_sync_state(
            code,
            "estimates",
            status="OK" if rows else "EMPTY",
            message="; ".join(warnings)[:1000] if warnings else None,
        )
        return {"rows": len(rows), "warnings": warnings}

    def _normalize_report_rc(
        self, code: str, raw_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not raw_rows:
            return []
        grouped: dict[tuple, list[dict[str, Any]]] = {}
        for raw in raw_rows:
            quarter = str(raw.get("quarter") or "")
            if "Q" not in quarter:
                continue
            year_text, quarter_text = quarter.split("Q", 1)
            try:
                fiscal_year = int(year_text)
                quarter_no = int(quarter_text)
            except ValueError:
                continue
            fiscal_period = {1: "Q1", 2: "H1", 3: "Q3", 4: "FY"}.get(quarter_no, "FY")
            report_date = parse_date(raw.get("report_date")) or now_cn()
            for metric, source_field, unit, scale in (
                ("revenue", "op_rt", "CNY", 10_000.0),
                ("operating_profit", "op_pr", "CNY", 10_000.0),
                ("net_income", "np", "CNY", 10_000.0),
                ("eps", "eps", "CNY/SHARE", 1.0),
                ("pe", "pe", "RATIO", 1.0),
            ):
                number = safe_float(raw.get(source_field))
                if number is None:
                    continue
                number *= scale
                key = (metric, fiscal_year, fiscal_period, report_date)
                grouped.setdefault(key, []).append(
                    {
                        "value": number,
                        "org": raw.get("org_name"),
                    }
                )

        # 同一 (metric, 年度, 预测期) 只保留最新研报日，避免把历史预测重复入库
        latest_dates: dict[tuple, datetime] = {}
        for (metric, fiscal_year, fiscal_period, report_date) in grouped:
            key = (metric, fiscal_year, fiscal_period)
            if key not in latest_dates or report_date > latest_dates[key]:
                latest_dates[key] = report_date

        rows = []
        for (metric, fiscal_year, fiscal_period, report_date), values in grouped.items():
            if latest_dates.get((metric, fiscal_year, fiscal_period)) != report_date:
                continue
            numbers = sorted(item["value"] for item in values)
            count = len(numbers)
            median = (
                numbers[count // 2]
                if count % 2 == 1
                else (numbers[count // 2 - 1] + numbers[count // 2]) / 2
            )
            rows.append(
                {
                    "as_of": report_date,
                    "code": code,
                    "metric": metric,
                    "fiscal_year": fiscal_year,
                    "fiscal_period": fiscal_period,
                    "horizon": fiscal_period,
                    "value_mean": sum(numbers) / count,
                    "value_median": median,
                    "value_high": max(numbers),
                    "value_low": min(numbers),
                    "analyst_count": len({item["org"] for item in values if item.get("org")}),
                    "unit": "CNY" if metric in ("revenue", "operating_profit", "net_income") else ("CNY/SHARE" if metric == "eps" else "RATIO"),
                    "source": "tushare_report_rc",
                    "source_updated_at": report_date,
                    "ingested_at": now_cn(),
                }
            )
        return rows

    # ------------------------------------------------------------------ 组合同步
    def sync_code(
        self,
        code: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        include_estimates: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"code": normalize_code(code)}
        try:
            result["profile"] = 1 if self.sync_profile(code) else 0
        except Exception as exc:
            result["profile_error"] = str(exc)
        try:
            result["statements"] = self.sync_statements(
                code, start_year=start_year, end_year=end_year, force=True
            )
        except Exception as exc:
            result["statements_error"] = str(exc)
        try:
            result["valuation"] = self.sync_valuation(code)
        except Exception as exc:
            result["valuation_error"] = str(exc)
        if include_estimates and self.config["financial"].get("estimate_enabled", True):
            try:
                result["estimates"] = self.sync_estimates(code)
            except Exception as exc:
                result["estimates_error"] = str(exc)

        # 任一核心数据集失败即视为失败：此前要求两个都失败才抛，导致"假成功"
        core_errors = {
            key: result.get(key)
            for key in ("profile_error", "statements_error", "valuation_error")
            if result.get(key)
        }
        if core_errors:
            detail = "; ".join(f"{key}={value}" for key, value in core_errors.items())
            raise RuntimeError(f"{code} 财务数据同步失败: {detail}")
        return result

    # ------------------------------------------------------------------ 查询
    #: 基础信息（含股本/市值）的 TTL：避免每次查询都打 Tushare/AkShare
    PROFILE_TTL_HOURS = 24
    #: 字段不全（如 total_mv 缺失）时的重试间隔，避免每次查询都重抓
    PROFILE_RETRY_MINUTES = 30

    def _profile_is_fresh(
        self, code: str, profile: dict[str, Any] | None = None
    ) -> bool:
        try:
            state = self.repository.get_sync_state(code, "profile")
        except Exception:
            return False
        if not state:
            return False
        updated = state.get("updated_at")
        status = str(state.get("status") or "")
        if status != "OK" or not isinstance(updated, datetime):
            return False
        age = (now_cn() - updated).total_seconds()
        current = (
            profile if profile is not None else (self.repository.get_profile(code) or {})
        )
        # 「同步成功」不等于「字段完整」：sync_stock_universe 只写名字/行业，
        # 股本/市值要靠 daily_basic 补。缺这些字段时允许半小时后重试，
        # 否则一次限流就会让 total_mv 空一整天。
        complete = bool(current.get("name")) and (
            current.get("total_mv") is not None
            or current.get("total_share") is not None
        )
        if complete:
            return age < self.PROFILE_TTL_HOURS * 3600
        return age < self.PROFILE_RETRY_MINUTES * 60

    def get_profile(self, code: str, *, auto_sync: bool = False) -> dict[str, Any]:
        code = normalize_code(code)
        profile = self.repository.get_profile(code)
        if auto_sync and (not profile or not self._profile_is_fresh(code, profile)):
            try:
                profile = self.sync_profile(code)
            except Exception as exc:
                if not profile:
                    raise
                logger.warning("基础信息刷新失败，返回库内旧数据 %s: %s", code, exc)
        if not profile:
            raise DataNotFoundError(f"未找到 {code} 的基础信息，请先同步", source="financial")
        boards = self.repository.get_board_membership(code)
        profile["boards"] = boards
        return profile

    def get_statements(
        self,
        code: str,
        statement_type: str = "IS",
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of: str | None = None,
        report_type: str | None = "consolidated",
        auto_sync: bool = True,
    ) -> list[dict[str, Any]]:
        statement_type = statement_type.upper()
        if statement_type not in ("IS", "BS", "CF"):
            raise ValueError("statement_type 必须是 IS / BS / CF")
        code = normalize_code(code)
        rows = self.repository.get_statements(
            code,
            statement_type,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            report_type=report_type,
        )

        if auto_sync:
            actual_periods = self.repository.get_statement_periods(
                code, statement_type, report_type=report_type or "consolidated"
            )
            start_dt = parse_date(start_date) if start_date else None
            end_dt = parse_date(end_date) if end_date else None
            if start_dt:
                start_year = start_dt.year
            elif actual_periods:
                start_year = min(actual_periods).year
            else:
                start_year = int(
                    self.config["financial"].get("statement_start_year", 2000)
                )
            end_year = end_dt.year if end_dt else now_cn().year
            missing = self._missing_statement_periods(
                code,
                statement_type,
                start_year,
                end_year,
                report_type=report_type or "consolidated",
            )
            if missing:
                self.sync_statements(
                    code,
                    start_year=min(period.year for period in missing),
                    end_year=end_year,
                    force=False,
                    report_type=report_type or "consolidated",
                )
                rows = self.repository.get_statements(
                    code,
                    statement_type,
                    start_date=start_date,
                    end_date=end_date,
                    as_of=as_of,
                    report_type=report_type,
                )
        return rows

    def sync_indicators(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """从**全量历史**报表重算指标并写回 fin_indicator。

        为什么忽略入参的日期范围：同比需要上年同期，只用窗口内数据会把已有
        ``revenue_yoy`` / ``net_income_yoy`` 覆盖成 None（DEDUP 是覆盖写）。
        全量重算是幂等的，代价只是一次多行 INSERT。
        """
        code = normalize_code(code)
        income = self.repository.get_statements(code, "IS")
        balance = self.repository.get_statements(code, "BS")
        cash = self.repository.get_statements(code, "CF")
        rows = self.metrics.build_indicator_rows(income, balance, cash, code=code)
        if not rows:
            return 0

        existing = {
            (row.get("period_end"), row.get("report_type")): row
            for row in self.repository.get_indicators(code)
        }
        now = now_cn()
        protected = 0
        for row in rows:
            key = (row.get("period_end"), row.get("report_type"))
            old = existing.get(key) or {}
            for column, value in list(row.items()):
                if value is None and old.get(column) is not None:
                    # 新算出来是空、库里已有有效值：保留旧值，避免"越同步越空"
                    row[column] = old[column]
                    protected += 1
            row["source"] = "derived"
            row["formula_version"] = self.metrics.formula_version
            row["ingested_at"] = now
        if protected:
            logger.info("%s 指标重算保留了 %s 个库里已有的非空值", code, protected)
        return self.repository.upsert_indicators(rows)

    def get_indicators(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        auto_sync: bool = True,
    ) -> list[dict[str, Any]]:
        code = normalize_code(code)
        rows = self.repository.get_indicators(
            code, start_date=start_date, end_date=end_date
        )
        if not auto_sync:
            return rows

        actual = self.repository.get_indicator_periods(code)
        start_dt = parse_date(start_date) if start_date else None
        end_dt = parse_date(end_date) if end_date else None
        if start_dt:
            start_year = start_dt.year
        elif actual:
            start_year = min(actual).year
        else:
            start_year = int(
                self.config["financial"].get("statement_start_year", 2000)
            )
        end_year = end_dt.year if end_dt else now_cn().year
        expected = set(self._expected_statement_periods(start_year, end_year))
        missing = expected - actual

        if missing:
            # 先确保缺失报告期的报表存在，再从数据库统一计算指标
            self.sync_statements(
                code,
                start_year=min(period.year for period in missing),
                end_year=end_year,
                force=False,
            )
            self.sync_indicators(code, start_date=start_date, end_date=end_date)
            rows = self.repository.get_indicators(
                code, start_date=start_date, end_date=end_date
            )
        return rows

    def get_valuation(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        auto_sync: bool = True,
    ) -> list[dict[str, Any]]:
        code = normalize_code(code)
        rows = self.repository.get_valuation(
            code, start_date=start_date, end_date=end_date
        )
        if auto_sync:
            should_sync = False
            if start_date and end_date:
                should_sync = bool(
                    self._missing_valuation_dates(code, start_date, end_date)
                )
            elif not rows:
                should_sync = True
            if should_sync:
                self.sync_valuation(
                    code, start_date=start_date, end_date=end_date, force=False
                )
                rows = self.repository.get_valuation(
                    code, start_date=start_date, end_date=end_date
                )
        return rows

    def get_estimates(
        self,
        code: str,
        *,
        fiscal_year: int | None = None,
        metric: str | None = None,
        auto_sync: bool = True,
    ) -> list[dict[str, Any]]:
        code = normalize_code(code)
        rows = self.repository.get_estimates(
            code, fiscal_year=fiscal_year, metric=metric
        )
        # 只有「该标的完全没有预期数据」才抓取；否则一次不存在的年份查询会
        # 白白消耗 report_rc 的每日 10 次配额。
        if auto_sync and self.config["financial"].get("estimate_enabled", True):
            has_any = bool(self.repository.get_estimates(code))
            if not has_any:
                result = self.sync_estimates(code)
                rows = self.repository.get_estimates(
                    code, fiscal_year=fiscal_year, metric=metric
                )
                if not rows and result.get("warnings") and not has_any:
                    raise DataNotFoundError(
                        "一致预期获取失败: " + "; ".join(result["warnings"]),
                        source="financial",
                    )
        return rows

    # ------------------------------------------------------------------ 内部
    def _write_raw_reports(
        self, statement_type: str, rows: list[dict[str, Any]], now: datetime
    ) -> None:
        if not rows:
            return
        existing = self.repository.get_raw_report_ids(
            rows[0]["code"], statement_type
        )
        raw_rows = []
        for row in rows:
            if row["report_id"] in existing:
                continue
            raw_rows.append(
                {
                    "fetched_at": now,
                    "report_id": row["report_id"],
                    "code": row["code"],
                    "statement_type": statement_type,
                    "period_end": row["period_end"],
                    "source": row["source"],
                    "payload": json.dumps(row, ensure_ascii=False, default=str),
                }
            )
        if raw_rows:
            self.repository.upsert_raw_reports(raw_rows)

    def _write_sync_state(
        self,
        code: str,
        dataset: str,
        *,
        status: str,
        last_period_end: datetime | None = None,
        last_publish_date: datetime | None = None,
        message: str | None = None,
    ) -> None:
        latest = self.repository.get_sync_state(code, dataset)
        if latest and (
            latest.get("status") == status
            and latest.get("last_period_end") == last_period_end
            and latest.get("last_publish_date") == last_publish_date
            and (latest.get("message") or None) == (message or None)
        ):
            return

        now = now_cn()
        self.repository.upsert_sync_state(
            [
                {
                    "updated_at": now,
                    "code": code,
                    "dataset": dataset,
                    "last_period_end": last_period_end,
                    "last_publish_date": last_publish_date,
                    "last_success_at": now if status == "OK" else None,
                    "status": status,
                    "message": message,
                }
            ]
        )
