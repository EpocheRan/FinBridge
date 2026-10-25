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
    BALANCE_COLUMNS,
    BOARD_COLUMNS,
    CASH_FLOW_COLUMNS,
    ESTIMATE_COLUMNS,
    FIELD_DICT_COLUMNS,
    INCOME_COLUMNS,
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

    def get_statements(
        self,
        code: str,
        statement_type: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        as_of: str | None = None,
        report_type: str | None = "consolidated",
    ) -> list[dict[str, Any]]:
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

        # 同一报告期取最新披露版本；同一版本优先合并报表
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row.get("period_end"), row.get("report_type"))
            if key not in latest:
                latest[key] = row
        return sorted(latest.values(), key=lambda item: item.get("period_end"))

    def get_latest_period(self, code: str, statement_type: str) -> datetime | None:
        table = STATEMENT_TYPE_TO_TABLE[statement_type]
        rows = self._query_dicts(
            f'SELECT MAX(period_end) AS period_end FROM "{table}" WHERE code = %s',
            (code,),
        )
        return rows[0]["period_end"] if rows else None

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
        rows = self._query_dicts(
            'SELECT * FROM "map_stock_board" WHERE code = %s ORDER BY ts DESC',
            (code,),
        )
        latest: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row.get("board_type"), row.get("board_name"))
            if key not in latest:
                latest[key] = row
        return sorted(
            latest.values(), key=lambda item: (item.get("board_type") or "", item.get("board_name") or "")
        )

    def get_sync_state(self, code: str, dataset: str) -> dict[str, Any] | None:
        rows = self._query_dicts(
            'SELECT * FROM "fin_sync_state" WHERE code = %s AND dataset = %s '
            "LATEST ON updated_at PARTITION BY code, dataset",
            (code, dataset),
        )
        return rows[0] if rows else None
