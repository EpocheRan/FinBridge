"""Screener 相关表结构（QuestDB）。

包含四张表：

- ``screener_daily``       日频筛选因子（业务表）
- ``intraday_baseline``    分时量能基线（业务表）
- ``screener_build_state`` 构建/重建状态（领域状态表）
- ``market_suspension``    停牌/退市/无数据负缓存（领域状态表）

设计约定：

- 业务表使用 WAL + DEDUP，重复计算幂等。
- ``screener_build_state`` 使用 append-only 方式记录状态快照，
  读取时通过 ``LATEST ON updated_at PARTITION BY build_id`` 取最新状态。
- ``market_suspension`` 同样 append-only，读取时按 code/market/status 取最新。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Scripts.ScreenerSchema")


def _ddl() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS "screener_daily" (
            ts TIMESTAMP,
            code SYMBOL,
            market SYMBOL,

            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            raw_close DOUBLE,
            adj_factor DOUBLE,
            prev_close DOUBLE,
            volume LONG,
            amount DOUBLE,
            turnover DOUBLE,

            pct_chg DOUBLE,
            gap_pct DOUBLE,
            intraday_pct DOUBLE,
            range_pct DOUBLE,

            avg_volume_5d DOUBLE,
            avg_volume_20d DOUBLE,
            avg_volume_60d DOUBLE,
            avg_amount_20d DOUBLE,
            volume_ratio_20d DOUBLE,
            volume_chg_pct DOUBLE,
            amount_ratio_20d DOUBLE,

            atr14 DOUBLE,
            atr_pct DOUBLE,
            volatility_20d DOUBLE,
            volatility_60d DOUBLE,

            ma5 DOUBLE,
            ma10 DOUBLE,
            ma20 DOUBLE,
            ma60 DOUBLE,
            ma120 DOUBLE,
            ma250 DOUBLE,
            close_vs_ma20_pct DOUBLE,
            close_vs_ma60_pct DOUBLE,
            close_vs_ma250_pct DOUBLE,

            high_20d DOUBLE,
            low_20d DOUBLE,
            high_52w DOUBLE,
            low_52w DOUBLE,
            dist_high_52w_pct DOUBLE,
            dist_low_52w_pct DOUBLE,
            is_new_high_52w BOOLEAN,
            is_new_low_52w BOOLEAN,

            benchmark_code SYMBOL,
            beta_252 DOUBLE,
            corr_252 DOUBLE,
            beta_window INT,

            total_share DOUBLE,
            float_share DOUBLE,
            total_mv DOUBLE,
            float_mv DOUBLE,

            lookback_days INT,
            is_partial BOOLEAN,
            core_ready BOOLEAN,
            full_ready BOOLEAN,
            formula_version STRING,
            computed_at TIMESTAMP
        ) TIMESTAMP(ts) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(ts, code, market)
        """,
        """
        CREATE TABLE IF NOT EXISTS "intraday_baseline" (
            as_of_date TIMESTAMP,
            code SYMBOL,
            market SYMBOL,
            freq SYMBOL,
            minute_end INT,
            session SYMBOL,

            avg_volume DOUBLE,
            avg_cum_volume DOUBLE,
            avg_amount DOUBLE,
            avg_cum_amount DOUBLE,
            std_cum_volume DOUBLE,

            sample_days INT,
            expected_days INT,
            coverage_ratio DOUBLE,
            includes_auction BOOLEAN,
            is_partial BOOLEAN,

            source_start TIMESTAMP,
            source_end TIMESTAMP,
            formula_version STRING,
            computed_at TIMESTAMP
        ) TIMESTAMP(as_of_date) PARTITION BY MONTH WAL
        DEDUP UPSERT KEYS(as_of_date, code, market, freq, minute_end)
        """,
        """
        CREATE TABLE IF NOT EXISTS "screener_build_state" (
            updated_at TIMESTAMP,
            build_id SYMBOL,
            build_kind SYMBOL,
            dataset SYMBOL,
            scope SYMBOL,
            scope_key SYMBOL,
            universe_key SYMBOL,
            universe_payload STRING,

            freq SYMBOL,
            formula_version STRING,

            window_start TIMESTAMP,
            window_end TIMESTAMP,

            status SYMBOL,
            quality SYMBOL,
            phase SYMBOL,

            cursor_ts TIMESTAMP,
            cursor_code SYMBOL,

            requested_codes INT,
            built_codes INT,
            coverage_ratio DOUBLE,

            progress_current LONG,
            progress_total LONG,
            rows_written LONG,

            attempts INT,
            max_attempts INT,
            message STRING,
            last_error STRING,
            retry_at TIMESTAMP,

            locked_by STRING,
            locked_until TIMESTAMP,

            created_at TIMESTAMP,
            finished_at TIMESTAMP
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, build_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS "market_suspension" (
            updated_at TIMESTAMP,
            code SYMBOL,
            market SYMBOL,
            status SYMBOL,

            start_date TIMESTAMP,
            end_date TIMESTAMP,
            retry_at TIMESTAMP,

            source SYMBOL,
            reason STRING,
            created_at TIMESTAMP
        ) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
        DEDUP UPSERT KEYS(updated_at, code, market, status, start_date)
        """,
    ]


def create_screener_tables(db, *, dry_run: bool = False) -> list[str]:
    statements = _ddl()
    for sql in statements:
        if dry_run:
            logger.info("[dry-run] %s", sql.strip().splitlines()[0])
        else:
            db.execute_ddl(sql)
    return statements
