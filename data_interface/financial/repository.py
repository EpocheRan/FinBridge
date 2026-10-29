"""财务数据 QuestDB 仓储层。

QuestDB 特性适配：

- 写入采用 append + ``DEDUP UPSERT KEYS``，不依赖 UPDATE。
- 报表以 ``period_end`` 为 designated timestamp，按年分区。
- 估值/预期以交易日/快照日为 designated timestamp，按月分区。
- 时点查询在 Python 层做“同一报告期取最新 publish_date”的去重，
  避免依赖 QuestDB 窗口函数的版本差异。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from data_interface.common.questdb_client import QuestDBClient

from .fields import (
    BOARD_COLUMNS,
    ESTIMATE_COLUMNS,
    FIELD_DICT_COLUMNS,
    INDICATOR_COLUMNS,
    PROFILE_COLUMNS,
    RAW_REPORT_COLUMNS,
    REPORT_META_COLUMNS,
    STATEMENT_COLUMNS,
    STATEMENT_TYPE_TO_TABLE,
    STOCK_BOARD_COLUMNS,
    SYNC_STATE_COLUMNS,
    VALUATION_COLUMNS,
)
from .normalizer import row_to_values

logger = logging.getLogger("DataInterface.FinancialRepository")


class FinancialRepository:
    def __init__(self, db: QuestDBClient | None = None):
        self.db = db or QuestDBClient()

    # ------------------------------------------------------------------ 写入
    def _insert(self, table: str, columns: list[str], rows: Iterable[dict[str, Any]]) -> int:
        rows = [row for row in rows if row]
        if not rows:
            return 0
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'
        values = [row_to_values(row, columns) for row in rows]
        return self.db.execute_many(sql, values)

    def upsert_profile(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("stock_basic_info", PROFILE_COLUMNS, rows)

    def upsert_boards(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("dim_board", BOARD_COLUMNS, rows)

    def upsert_stock_boards(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("map_stock_board", STOCK_BOARD_COLUMNS, rows)

    def upsert_report_meta(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_report_meta", REPORT_META_COLUMNS, rows)

    def upsert_statements(self, statement_type: str, rows: Iterable[dict[str, Any]]) -> int:
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        columns = STATEMENT_COLUMNS[statement_type]
        return self._insert(table, columns, rows)

    def upsert_estimates(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_estimate", ESTIMATE_COLUMNS, rows)

    def upsert_indicators(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_indicator", INDICATOR_COLUMNS, rows)

    def upsert_valuation(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_valuation_daily", VALUATION_COLUMNS, rows)

    def upsert_raw_reports(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_raw_report", RAW_REPORT_COLUMNS, rows)

    def upsert_sync_state(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("fin_sync_state", SYNC_STATE_COLUMNS, rows)

    def upsert_field_dict(self, rows: Iterable[dict[str, Any]]) -> int:
        return self._insert("dim_fin_field", FIELD_DICT_COLUMNS, rows)

    # ------------------------------------------------------------------ 查询
    def _query_dicts(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        self.db.ensure_connected()
        with self.db._conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def get_profile(self, code: str) -> dict[str, Any] | None:
        rows = self._query_dicts(
            'SELECT * FROM "stock_basic_info" WHERE code = %s '
            "LATEST ON ts PARTITION BY code",
            (code,),
        )
        return rows[0] if rows else None

    def get_fund_codes(self) -> list[str]:
        rows = self._query_dicts(
            'SELECT code FROM "fund_basic_info" LATEST ON ts PARTITION BY code'
        )
        return sorted({row["code"] for row in rows if row.get("code")})

    def get_profile_codes(self, active_only: bool = True) -> list[str]:
        sql = 'SELECT code FROM "stock_basic_info"'
        if active_only:
            sql += " WHERE list_status = 'L'"
        sql += " LATEST ON ts PARTITION BY code"
        rows = self._query_dicts(sql)
        return sorted({row["code"] for row in rows if row.get("code")})

    def get_statements(
        self,
        code: str,
        statement_type: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of: str | None = None,
        report_type: str | None = "consolidated",
        latest_per_period: bool = True,
    ) -> list[dict[str, Any]]:
        """查询报表。

        Args:
            latest_per_period:
                True（默认）= 每个报告期只返回最新披露版本（面向用户查询）；
                False = 返回**全部版本**，供 Point-in-Time 计算按 ``as_of``
                选择当时已知的那一版（否则重述前的历史估值会少一个季度）。
        """
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        conditions = ["code = %s"]
        params: list[Any] = [code]
        if start_date:
            conditions.append("period_end >= CAST(%s AS TIMESTAMP)")
            params.append(start_date)
        if end_date:
            conditions.append("period_end <= CAST(%s AS TIMESTAMP)")
            params.append(end_date)
        if as_of:
            conditions.append("publish_date <= CAST(%s AS TIMESTAMP)")
            params.append(as_of)
        if report_type:
            conditions.append("report_type = %s")
            params.append(report_type)
        sql = (
            f'SELECT * FROM "{table}" WHERE ' + " AND ".join(conditions) +
            " ORDER BY period_end DESC, publish_date DESC"
        )
        rows = self._query_dicts(sql, tuple(params))
        if not latest_per_period:
            return sorted(rows, key=lambda item: (item.get("period_end"), item.get("publish_date") or 0))

        # 同一报告期取最新披露版本；同一版本优先合并报表
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row.get("period_end"), row.get("report_type"))
            if key not in latest:
                latest[key] = row
        return sorted(latest.values(), key=lambda item: item.get("period_end"))

    def get_statements_bulk(
        self,
        statement_type: str,
        as_of: datetime,
        *,
        report_type: str = "consolidated",
    ) -> list[dict[str, Any]]:
        """一次取回某时点可见的全部报表版本（按 code 分组用）。

        供「按交易日全市场批量估值」使用：用一次查询代替逐只
        ``get_statements``，避免 5000 次单票往返。
        """
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        sql = (
            f'SELECT * FROM "{table}" '
            "WHERE publish_date <= CAST(%s AS TIMESTAMP) "
            "AND report_type = %s "
            "ORDER BY code, period_end, publish_date"
        )
        return self._query_dicts(sql, (as_of, report_type))

    def get_latest_period(self, code: str, statement_type: str) -> datetime | None:
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        rows = self._query_dicts(
            f'SELECT MAX(period_end) AS period_end FROM "{table}" WHERE code = %s',
            (code,),
        )
        return rows[0]["period_end"] if rows else None

    def get_statement_periods(
        self,
        code: str,
        statement_type: str,
        *,
        report_type: str = "consolidated",
    ) -> set[datetime]:
        """返回数据库中已有报告期，用于计算缺失区间。"""
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        rows = self._query_dicts(
            f'SELECT DISTINCT period_end FROM "{table}" '
            "WHERE code = %s AND report_type = %s",
            (code, report_type),
        )
        return {row["period_end"] for row in rows if row.get("period_end")}

    def get_raw_report_ids(self, code: str, statement_type: str) -> set[str]:
        rows = self._query_dicts(
            'SELECT DISTINCT report_id FROM "fin_raw_report" '
            "WHERE code = %s AND statement_type = %s",
            (code, statement_type),
        )
        return {row["report_id"] for row in rows if row.get("report_id")}

    def get_valuation_dates(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> set:
        conditions = ["code = %s"]
        params: list[Any] = [code]
        if start_date:
            conditions.append("ts >= CAST(%s AS TIMESTAMP)")
            params.append(start_date)
        if end_date:
            conditions.append("ts <= CAST(%s AS TIMESTAMP)")
            params.append(end_date)
        rows = self._query_dicts(
            'SELECT DISTINCT ts FROM "fin_valuation_daily" WHERE '
            + " AND ".join(conditions),
            tuple(params),
        )
        return {row["ts"].date() for row in rows if row.get("ts")}

    def get_indicators(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["code = %s"]
        params: list[Any] = [code]
        if start_date:
            conditions.append("period_end >= CAST(%s AS TIMESTAMP)")
            params.append(start_date)
        if end_date:
            conditions.append("period_end <= CAST(%s AS TIMESTAMP)")
            params.append(end_date)
        rows = self._query_dicts(
            'SELECT * FROM "fin_indicator" WHERE ' + " AND ".join(conditions) +
            " ORDER BY period_end DESC, report_type DESC",
            tuple(params),
        )
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row.get("period_end"), row.get("report_type"))
            if key not in latest:
                latest[key] = row
        return sorted(latest.values(), key=lambda item: item["period_end"])

    def get_indicator_periods(self, code: str) -> set[datetime]:
        rows = self._query_dicts(
            'SELECT DISTINCT period_end FROM "fin_indicator" WHERE code = %s',
            (code,),
        )
        return {row["period_end"] for row in rows if row.get("period_end")}

    def get_valuation_latest_ts(self) -> datetime | None:
        rows = self._query_dicts('SELECT max(ts) AS ts FROM "fin_valuation_daily"')
        return rows[0]["ts"] if rows and rows[0].get("ts") else None

    def get_valuation(
        self,
        code: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["code = %s"]
        params: list[Any] = [code]
        if start_date:
            conditions.append("ts >= CAST(%s AS TIMESTAMP)")
            params.append(start_date)
        if end_date:
            conditions.append("ts <= CAST(%s AS TIMESTAMP)")
            params.append(end_date)
        sql = (
            'SELECT * FROM "fin_valuation_daily" WHERE ' + " AND ".join(conditions) +
            " ORDER BY ts"
        )
        return self._query_dicts(sql, tuple(params))

    def get_estimates(
        self,
        code: str,
        *,
        fiscal_year: int | None = None,
        metric: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["code = %s"]
        params: list[Any] = [code]
        if fiscal_year is not None:
            conditions.append("fiscal_year = %s")
            params.append(fiscal_year)
        if metric:
            conditions.append("metric = %s")
            params.append(metric)
        sql = (
            'SELECT * FROM "fin_estimate" WHERE ' + " AND ".join(conditions) +
            " ORDER BY fiscal_year, metric, as_of DESC"
        )
        rows = self._query_dicts(sql, tuple(params))
        # 同一 (metric, fiscal_year, fiscal_period) 取最新 as_of
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row.get("metric"), row.get("fiscal_year"), row.get("fiscal_period"))
            if key not in latest:
                latest[key] = row
        return list(latest.values())

    def get_board_membership(self, code: str) -> list[dict[str, Any]]:
        # 板块归属是「追加快照」语义：用 LATEST ON 只取每个板块的最新一行，
        # 避免随使用天数增长而全表扫描（旧实现把所有历史版本拉回 Python 去重）
        rows = self._query_dicts(
            'SELECT * FROM "map_stock_board" WHERE code = %s '
            "LATEST ON ts PARTITION BY board_id",
            (code,),
        )
        return sorted(
            rows, key=lambda item: (item.get("board_type") or "", item.get("board_name") or "")
        )

    def get_sync_state(self, code: str, dataset: str) -> dict[str, Any] | None:
        rows = self._query_dicts(
            'SELECT * FROM "fin_sync_state" WHERE code = %s AND dataset = %s '
            "LATEST ON updated_at PARTITION BY code, dataset",
            (code, dataset),
        )
        return rows[0] if rows else None
