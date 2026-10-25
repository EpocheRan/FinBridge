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
from datetime import datetime, timedelta
from typing import Any

from data_interface.common.config import get_config
from data_interface.common.exceptions import (
    DailyQuotaExceededError,
    DataNotFoundError,
    PermissionDeniedError,
    RateLimitExceededError,
)
from data_interface.common.questdb_client import QuestDBClient

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
from .sources.tushare_source import TushareSource

logger = logging.getLogger("DataInterface.FinancialService")


def _infer_exchange(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return "SSE"
    if code.startswith(("4", "8")):
        return "BSE"
    return "SZSE"


def _infer_board(code: str) -> str:
    if code.startswith("688"):
        return "STAR"
    if code.startswith(("300", "301")):
        return "GEM"
    if code.startswith(("4", "8")):
        return "BSE"
    return "MAIN"


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

    @property
    def tushare(self) -> TushareSource:
        if self._tushare is None:
            self._tushare = TushareSource(self.config)
        return self._tushare

    # ------------------------------------------------------------------ 基础信息
    def sync_profile(self, code: str) -> dict[str, Any]:
        code = normalize_code(code)
        now = datetime.utcnow()
        profile: dict[str, Any] = {
            "ts": now,
            "code": code,
            "exchange": _infer_exchange(code),
            "board": _infer_board(code),
            "market": "A",
            "security_type": "STOCK",
            "currency": "CNY",
            "source": "tushare+akshare",
            "updated_at": now,
        }

        tushare_error: Exception | None = None
        try:
            rows = self.tushare.fetch_stock_basic(code=code, list_status="L")
            if rows:
                raw = rows[0]
                profile.update(
                    {
                        "name": raw.get("name"),
                        "fullname": raw.get("fullname"),
                        "exchange": raw.get("exchange") or profile["exchange"],
                        "board": _map_board(raw.get("market"), code),
                        "list_status": raw.get("list_status"),
                        "list_date": parse_date(raw.get("list_date")),
                        "delist_date": parse_date(raw.get("delist_date")),
                        "industry": raw.get("industry"),
                        "area": raw.get("area"),
                    }
                )
        except Exception as exc:
            tushare_error = exc
            logger.warning("Tushare stock_basic 失败 %s: %s", code, exc)

        # 用最近一个交易日的 daily_basic 补齐股本/市值，避免依赖 AkShare 网页接口
        try:
            today = datetime.now()
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
                        "ts": now,
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

    # ------------------------------------------------------------------ 板块
    def sync_board_catalog(self, board_types: tuple[str, ...] = ("industry", "concept")) -> int:
        """同步东财行业/概念板块目录。"""
        total = 0
        now = datetime.utcnow()
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
        now = datetime.utcnow()
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

    # ------------------------------------------------------------------ 三大报表
    def sync_statements(
        self,
        code: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> dict[str, int]:
        code = normalize_code(code)
        start_year = start_year or int(
            self.config["financial"].get("statement_start_year", 2000)
        )
        end_year = end_year or datetime.now().year
        start_date = f"{start_year}0101"
        end_date = f"{end_year}1231"

        raw_income = self.tushare.fetch_income(
            code, start_date=start_date, end_date=end_date
        )
        raw_balance = self.tushare.fetch_balance_sheet(
            code, start_date=start_date, end_date=end_date
        )
        raw_cash = self.tushare.fetch_cash_flow(
            code, start_date=start_date, end_date=end_date
        )
        raw_fina = self.tushare.fetch_fina_indicator(
            code, start_date=start_date, end_date=end_date
        )

        now = datetime.utcnow()
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
    ) -> int:
        code = normalize_code(code)
        end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        start_date = start_date or (
            f"{self.config['financial'].get('valuation_start_year', 2020)}-01-01"
        )

        daily_rows = self.tushare.fetch_daily_basic(
            code,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )
        if not daily_rows:
            return 0

        income_rows = self.repository.get_statements(code, "IS")
        balance_rows = self.repository.get_statements(code, "BS")
        cash_rows = self.repository.get_statements(code, "CF")
        if not income_rows:
            self.sync_statements(code)
            income_rows = self.repository.get_statements(code, "IS")
            balance_rows = self.repository.get_statements(code, "BS")
            cash_rows = self.repository.get_statements(code, "CF")

        now = datetime.utcnow()
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
        self._write_sync_state(
            code,
            "valuation",
            status="OK",
            last_period_end=output[-1]["ts"] if output else None,
        )
        return count

    @staticmethod
    def _visible_as_of(rows: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
        visible = []
        for row in rows:
            publish_date = row.get("publish_date")
            # 没有披露日的数据无法做时点判断，估值计算中直接排除，避免前视偏差
            if publish_date is None or publish_date > as_of:
                continue
            visible.append(row)
        return visible

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
            report_date = parse_date(raw.get("report_date")) or datetime.utcnow()
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
                    "ingested_at": datetime.utcnow(),
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
                code, start_year=start_year, end_year=end_year
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

        core_errors = [
            result.get("statements_error"),
            result.get("valuation_error"),
        ]
        if all(core_errors):
            raise RuntimeError(
                f"{code} 财务数据同步失败: statements={core_errors[0]}; valuation={core_errors[1]}"
            )
        return result

    # ------------------------------------------------------------------ 查询
    def get_profile(self, code: str, *, auto_sync: bool = False) -> dict[str, Any]:
        code = normalize_code(code)
        profile = self.repository.get_profile(code)
        if auto_sync and (not profile or not profile.get("exchange") or not profile.get("board")):
            profile = self.sync_profile(code)
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
        rows = self.repository.get_statements(
            normalize_code(code),
            statement_type,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            report_type=report_type,
        )
        if not rows and auto_sync:
            self.sync_statements(normalize_code(code))
            rows = self.repository.get_statements(
                normalize_code(code),
                statement_type,
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
                report_type=report_type,
            )
        return rows

    def get_indicators(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        auto_sync: bool = True,
    ) -> list[dict[str, Any]]:
        code = normalize_code(code)
        income = self.get_statements(
            code, "IS", start_date=start_date, end_date=end_date, auto_sync=auto_sync
        )
        balance = self.get_statements(
            code, "BS", start_date=start_date, end_date=end_date, auto_sync=auto_sync
        )
        cash = self.get_statements(
            code, "CF", start_date=start_date, end_date=end_date, auto_sync=auto_sync
        )
        return self.metrics.build_indicator_rows(income, balance, cash)

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
        if not rows and auto_sync:
            self.sync_valuation(code, start_date=start_date, end_date=end_date)
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
        if not rows and auto_sync and self.config["financial"].get(
            "estimate_enabled", True
        ):
            result = self.sync_estimates(code)
            rows = self.repository.get_estimates(
                code, fiscal_year=fiscal_year, metric=metric
            )
            if not rows and result.get("warnings"):
                raise DataNotFoundError(
                    "一致预期获取失败: " + "; ".join(result["warnings"]),
                    source="financial",
                )
        return rows

    # ------------------------------------------------------------------ 内部
    def _write_raw_reports(
        self, statement_type: str, rows: list[dict[str, Any]], now: datetime
    ) -> None:
        raw_rows = []
        for row in rows:
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
        now = datetime.utcnow()
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
