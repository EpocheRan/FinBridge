"""财务相关表结构（QuestDB）。

设计要点：
- 报表用 ``period_end`` 作为 designated timestamp，按年分区；
  ``publish_date`` 保留为普通 TIMESTAMP，用于 Point-in-Time。
- 一致预期用 ``as_of`` 作为 designated timestamp，按月分区。
- 估值用交易日 ``ts``，按月分区。
- 所有可变表使用 WAL + DEDUP UPSERT KEYS，重复同步天然幂等。
"""

from __future__ import annotations

import logging
from datetime import datetime

from data_interface.financial.fields import (
    FIELD_DEFINITIONS,
    FIELD_DICT_COLUMNS,
    TUSHARE_BALANCE_MAP,
    TUSHARE_CASH_FLOW_MAP,
    TUSHARE_FINA_INDICATOR_MAP,
    TUSHARE_INCOME_MAP,
)

logger = logging.getLogger("Scripts.FinancialSchema")


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "fin_report_meta" (
            period_end TIMESTAMP,
            report_id SYMBOL,
            code SYMBOL,
            statement_type SYMBOL,
            period_type SYMBOL,
            report_type SYMBOL,
            fiscal_year INT,
            publish_date TIMESTAMP,
            currency SYMBOL,
            unit_scale DOUBLE,
            source SYMBOL,
            source_version STRING,
            is_restated BOOLEAN,
            quality_status SYMBOL,
            ingested_at TIMESTAMP
        ) TIMESTAMP(period_end) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(period_end, report_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_income_statement" (
            period_end TIMESTAMP,
            report_id SYMBOL,
            code SYMBOL,
            publish_date TIMESTAMP,
            period_type SYMBOL,
            report_type SYMBOL,
            fiscal_year INT,
            total_revenue DOUBLE,
            revenue DOUBLE,
            cost_of_revenue DOUBLE,
            gross_profit DOUBLE,
            research_and_development_expense DOUBLE,
            selling_expense DOUBLE,
            general_and_admin_expense DOUBLE,
            selling_general_admin_expense DOUBLE,
            finance_expense DOUBLE,
            interest_expense DOUBLE,
            depreciation_and_amortization DOUBLE,
            operating_profit DOUBLE,
            ebit DOUBLE,
            ebitda DOUBLE,
            pretax_profit DOUBLE,
            income_tax DOUBLE,
            net_income DOUBLE,
            net_income_attributable_to_parent DOUBLE,
            minority_interest DOUBLE,
            continuing_operations_net_income DOUBLE,
            discontinued_operations_net_income DOUBLE,
            basic_eps DOUBLE,
            diluted_eps DOUBLE,
            dividend_per_share DOUBLE,
            source SYMBOL,
            quality_status SYMBOL,
            ingested_at TIMESTAMP
        ) TIMESTAMP(period_end) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(period_end, report_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_balance_sheet" (
            period_end TIMESTAMP,
            report_id SYMBOL,
            code SYMBOL,
            publish_date TIMESTAMP,
            period_type SYMBOL,
            report_type SYMBOL,
            fiscal_year INT,
            cash_and_cash_equivalents DOUBLE,
            short_term_investments DOUBLE,
            cash_and_short_term_investments DOUBLE,
            accounts_receivable DOUBLE,
            inventory DOUBLE,
            total_current_assets DOUBLE,
            goodwill DOUBLE,
            intangible_assets DOUBLE,
            fixed_assets DOUBLE,
            total_non_current_assets DOUBLE,
            total_assets DOUBLE,
            short_term_debt DOUBLE,
            current_portion_of_long_term_debt DOUBLE,
            accounts_payable DOUBLE,
            total_current_liabilities DOUBLE,
            long_term_debt DOUBLE,
            bonds_payable DOUBLE,
            total_non_current_liabilities DOUBLE,
            total_liabilities DOUBLE,
            total_debt DOUBLE,
            net_debt DOUBLE,
            total_equity DOUBLE,
            equity_attributable_to_parent DOUBLE,
            minority_equity DOUBLE,
            retained_earnings DOUBLE,
            source SYMBOL,
            quality_status SYMBOL,
            ingested_at TIMESTAMP
        ) TIMESTAMP(period_end) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(period_end, report_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_cash_flow" (
            period_end TIMESTAMP,
            report_id SYMBOL,
            code SYMBOL,
            publish_date TIMESTAMP,
            period_type SYMBOL,
            report_type SYMBOL,
            fiscal_year INT,
            operating_cash_flow DOUBLE,
            investing_cash_flow DOUBLE,
            financing_cash_flow DOUBLE,
            capital_expenditure DOUBLE,
            free_cash_flow DOUBLE,
            depreciation_and_amortization DOUBLE,
            change_in_working_capital DOUBLE,
            dividends_paid DOUBLE,
            net_change_in_cash DOUBLE,
            cash_at_beginning DOUBLE,
            cash_at_end DOUBLE,
            source SYMBOL,
            quality_status SYMBOL,
            ingested_at TIMESTAMP
        ) TIMESTAMP(period_end) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(period_end, report_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_estimate" (
            as_of TIMESTAMP,
            code SYMBOL,
            metric SYMBOL,
            fiscal_year INT,
            fiscal_period SYMBOL,
            horizon SYMBOL,
            value_mean DOUBLE,
            value_median DOUBLE,
            value_high DOUBLE,
            value_low DOUBLE,
            analyst_count INT,
            unit SYMBOL,
            source SYMBOL,
            source_updated_at TIMESTAMP,
            ingested_at TIMESTAMP
        ) TIMESTAMP(as_of) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(code, metric, fiscal_year, fiscal_period, as_of, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_valuation_daily" (
            ts TIMESTAMP,
            code SYMBOL,
            close DOUBLE,
            total_share DOUBLE,
            market_cap DOUBLE,
            float_market_cap DOUBLE,
            enterprise_value DOUBLE,
            pe_ttm DOUBLE,
            pb_mrq DOUBLE,
            ps_ttm DOUBLE,
            pcf_ttm DOUBLE,
            pfcf DOUBLE,
            ev_ebitda DOUBLE,
            ev_ebit DOUBLE,
            ev_sales DOUBLE,
            ev_gross_profit DOUBLE,
            ev_fcf DOUBLE,
            p_nwc DOUBLE,
            earnings_yield DOUBLE,
            dividend_yield DOUBLE,
            market_cap_change_pct DOUBLE,
            source SYMBOL,
            formula_version STRING,
            ingested_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, code, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_sync_state" (
            updated_at TIMESTAMP,
            code SYMBOL,
            dataset SYMBOL,
            last_period_end TIMESTAMP,
            last_publish_date TIMESTAMP,
            last_success_at TIMESTAMP,
            status SYMBOL,
            message STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, code, dataset)
        """,
        """
        CREATE TABLE IF NOT EXISTS "fin_raw_report" (
            fetched_at TIMESTAMP,
            report_id SYMBOL,
            code SYMBOL,
            statement_type SYMBOL,
            period_end TIMESTAMP,
            source SYMBOL,
            payload STRING
        ) TIMESTAMP(fetched_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(fetched_at, report_id, source)
        """,
        """
        CREATE TABLE IF NOT EXISTS "dim_fin_field" (
            updated_at TIMESTAMP,
            canonical_name SYMBOL,
            display_cn STRING,
            statement_type SYMBOL,
            data_type SYMBOL,
            unit SYMBOL,
            sign_convention SYMBOL,
            formula STRING,
            aliases STRING,
            description STRING
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, canonical_name, statement_type)
        """,
        """
        CREATE TABLE IF NOT EXISTS "dim_board" (
            ts TIMESTAMP,
            board_id SYMBOL,
            board_type SYMBOL,
            board_name STRING,
            parent_id SYMBOL,
            source SYMBOL
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(board_id, board_type, source, ts)
        """,
        """
        CREATE TABLE IF NOT EXISTS "map_stock_board" (
            ts TIMESTAMP,
            code SYMBOL,
            board_id SYMBOL,
            board_type SYMBOL,
            board_name STRING,
            is_primary BOOLEAN,
            effective_from TIMESTAMP,
            effective_to TIMESTAMP,
            source SYMBOL
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(code, board_id, ts)
        """,
    ]


def _seed_field_dict(db, *, dry_run: bool = False) -> None:
    now = datetime.utcnow()
    columns = FIELD_DICT_COLUMNS
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f'INSERT INTO "dim_fin_field" ({quoted}) VALUES ({placeholders})'
    source_maps = {
        "IS": TUSHARE_INCOME_MAP,
        "BS": TUSHARE_BALANCE_MAP,
        "CF": TUSHARE_CASH_FLOW_MAP,
        "COMMON": TUSHARE_FINA_INDICATOR_MAP,
    }
    rows = []
    for item in FIELD_DEFINITIONS:
        aliases = [
            source_field
            for source_field, canonical in source_maps.get(
                item["statement_type"], {}
            ).items()
            if canonical == item["canonical_name"]
        ]
        row = {
            **item,
            "updated_at": now,
            "aliases": ",".join(sorted(aliases)),
        }
        rows.append(tuple(row.get(column) for column in columns))
    if dry_run:
        logger.info("[dry-run] seed dim_fin_field: %s rows", len(rows))
        return
    # 字段字典是静态维度表；重复初始化时清空后重建，避免快照无限增长
    try:
        db.execute('TRUNCATE TABLE "dim_fin_field"')
    except Exception as exc:
        logger.warning("清空 dim_fin_field 失败，将追加写入: %s", exc)
    db.execute_many(sql, rows)


def create_financial_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    _seed_field_dict(db, dry_run=dry_run)
    return statements

