"""把已校验的 ``ScreenSpec`` 转成 QuestDB SQL。"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from data_interface.common.questdb_client import QuestDBClient
from screener.domain.fields import resolve_field
from screener.domain.models import Filter, ScreenSpec


DERIVED_SQL = {
    "price_x_avg_volume": "close * avg_volume_20d",
    "price_x_volume": "close * volume",
    "pct_from_open": "(close / open - 1) * 100",
    "dist_high_20d_pct": "(close / high_20d - 1) * 100",
    "dist_low_20d_pct": "(close / low_20d - 1) * 100",
}


class QuestDBScreenerQuery:
    def __init__(self, db: QuestDBClient):
        self.db = db

    def screen(self, spec: ScreenSpec) -> pd.DataFrame:
        if spec.as_of is None:
            return pd.DataFrame()
        sql, params = self.build_sql(spec)
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        if spec.offset:
            frame = frame.iloc[int(spec.offset) :]
        return frame

    def build_sql(self, spec: ScreenSpec) -> tuple[str, list]:
        derived = [
            f"{expression} AS {name}"
            for name, expression in DERIVED_SQL.items()
        ]
        select = "SELECT *, " + ", ".join(derived) + ' FROM "factor_daily_wide"'

        clauses = ["ts = %s", "market = %s"]
        params: list = [
            datetime.combine(spec.as_of, time.min),
            spec.universe.market,
        ]

        codes = spec.universe.normalized_codes()
        if codes is not None and not spec.universe.all_market:
            placeholders = ", ".join(["%s"] * len(codes))
            clauses.append(f"code IN ({placeholders})")
            params.extend(codes)

        for item in spec.filters:
            expression, values = self._filter(item)
            clauses.append(expression)
            params.extend(values)

        if not spec.allow_partial and not any(
            item.field == "is_partial" for item in spec.filters
        ):
            clauses.append("is_partial = false")

        sql = select + " WHERE " + " AND ".join(clauses)
        if spec.sort:
            ordering = ", ".join(
                f"{self._expression(item.field)} {'DESC' if item.desc else 'ASC'}"
                for item in spec.sort
            )
        else:
            ordering = "code ASC"
        sql += f" ORDER BY {ordering} LIMIT {int(spec.limit) + int(spec.offset)}"
        return sql, params

    def _expression(self, name: str) -> str:
        field = resolve_field(name)
        if field.kind == "derived":
            return DERIVED_SQL[name]
        return name

    def _filter(self, item: Filter) -> tuple[str, list]:
        field = resolve_field(item.field)
        expression = self._expression(item.field)
        op = item.op
        if op == "eq":
            return f"{expression} = %s", [item.value]
        if op == "ne":
            return f"{expression} <> %s", [item.value]
        if op == "gt":
            return f"{expression} > %s", [item.value]
        if op == "ge":
            return f"{expression} >= %s", [item.value]
        if op == "lt":
            return f"{expression} < %s", [item.value]
        if op == "le":
            return f"{expression} <= %s", [item.value]
        if op == "between":
            return (
                f"{expression} >= %s AND {expression} <= %s",
                [item.value[0], item.value[1]],
            )
        if op == "in":
            values = list(item.value)
            placeholders = ", ".join(["%s"] * len(values))
            return f"{expression} IN ({placeholders})", values
        if op == "is_null":
            return f"{expression} IS NULL", []
        if op == "not_null":
            return f"{expression} IS NOT NULL", []
        # ScreenSpec.validate 已经拦截；这里兜底。
        raise ValueError(f"不支持的操作符: {op} ({field.name})")
