from __future__ import annotations

from screener.domain.models import ScreenSpec
from screener.infrastructure.query_builder import QuestDBScreenerQuery


def test_query_builder_omits_code_filter_for_all_market():
    query = QuestDBScreenerQuery(None)
    spec = ScreenSpec.from_dict(
        {
            "as_of": "2026-10-23",
            "universe": {"all_market": True, "codes": None},
            "filters": [{"field": "beta_252", "op": "between", "value": [0.8, 1.5]}],
            "sort": [{"field": "pct_chg", "desc": True}],
            "limit": 50,
            "offset": 10,
        }
    )
    sql, params = query.build_sql(spec)
    assert "code IN" not in sql
    assert "beta_252 >= %s AND beta_252 <= %s" in sql
    assert "LIMIT 60" in sql
    assert "OFFSET" not in sql
    assert params[0].year == 2026


def test_query_builder_uses_derived_expression_and_codes():
    query = QuestDBScreenerQuery(None)
    spec = ScreenSpec.from_dict(
        {
            "as_of": "2026-10-23",
            "universe": {"codes": ["600519"]},
            "filters": [
                {"field": "price_x_avg_volume", "op": "gt", "value": 1_000_000}
            ],
            "sort": [{"field": "price_x_avg_volume", "desc": False}],
            "limit": 10,
        }
    )
    sql, params = query.build_sql(spec)
    assert "code IN (%s)" in sql
    assert "close * avg_volume_20d" in sql
    assert "ORDER BY close * avg_volume_20d ASC" in sql
    assert params[2] == "600519"
