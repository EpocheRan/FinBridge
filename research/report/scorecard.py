"""因子评分卡：把每次评级结果落到 ``factor_scorecard`` 表。

用途
----
- 记录全量因子的评级/评分，支持跨期对比（同一因子每季度重跑，看评级迁移）；
- 记录**校准方向**：设计方向与实测 IC 符号不一致时，标记 ``calibrated_direction``，
  供 ``--apply`` 回写 registry。

表结构
------
``factor_scorecard(ts, run_id, factor_id, category, grade, score, time_score, space_score,
ic_mean, icir, nw_t, ic_win, mono, turnover, coverage, direction, calibrated_direction,
quadrant, veto, sample_start, sample_end, computed_at)``
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

from data_interface.common.config import get_config, resolve_questdb_host
from data_interface.common.timeutil import now_cn

logger = logging.getLogger("Research.Scorecard")

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"

DDL = """
CREATE TABLE IF NOT EXISTS factor_scorecard (
    ts TIMESTAMP,
    run_id SYMBOL,
    factor_id SYMBOL,
    category SYMBOL,
    grade SYMBOL,
    score DOUBLE,
    time_score DOUBLE,
    space_score DOUBLE,
    ic_mean DOUBLE,
    icir DOUBLE,
    nw_t DOUBLE,
    ic_win DOUBLE,
    mono DOUBLE,
    turnover DOUBLE,
    coverage DOUBLE,
    direction LONG,
    calibrated_direction LONG,
    quadrant SYMBOL,
    veto VARCHAR,
    sample_start TIMESTAMP,
    sample_end TIMESTAMP,
    computed_at TIMESTAMP
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, run_id, factor_id)
""".strip()


def _exec(sql: str, timeout: int = 120) -> None:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"{payload['error']} :: {sql[:200]}")


def ensure_table() -> None:
    _exec(DDL)


def _q(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "null" if value != value else repr(float(value))
    text = str(value).replace("'", "''").replace("\\", "\\\\")
    return f"'{text}'"


def _ts(value) -> str:
    if value is None:
        return "null"
    return f"'{pd_timestamp(value)}'"


def pd_timestamp(value) -> str:
    import pandas as pd

    return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def save(report, *, run_id: str, category: str = "", horizon: int = 5, score: dict | None = None) -> None:
    """把一份 FactorReport 写入 scorecard（按 ts+run_id+factor_id 去重）。"""
    ensure_table()
    rating = score if score is not None else (report.rating or {})
    detail = rating.get("detail", {}) if isinstance(rating, dict) else {}
    series = report.ic.get(horizon)
    ic_mean = float(series.mean()) if series is not None and len(series) else float("nan")
    icir = float(detail.get("icir", float("nan")))
    nw_t = float(detail.get("nw_t", float("nan")))
    ic_win = float(detail.get("ic_win", float("nan")))
    mono = float(report.monotonicity) if report.monotonicity == report.monotonicity else float("nan")
    turnover = float(report.turnover) if report.turnover == report.turnover else float("nan")
    calibrated = 1 if ic_mean >= 0 else -1
    vetoes = " | ".join(report.vetoes) if getattr(report, "vetoes", None) else ""
    stamp = pd_timestamp(now_cn())
    values = ", ".join(
        [
            _ts(now_cn()),
            _q(run_id),
            _q(report.factor_id),
            _q(category),
            _q(rating.get("grade", "")),
            _q(rating.get("score", float("nan"))),
            _q(rating.get("time_score", float("nan"))),
            _q(rating.get("space_score", float("nan"))),
            _q(ic_mean),
            _q(icir),
            _q(nw_t),
            _q(ic_win),
            _q(mono),
            _q(turnover),
            _q(report.coverage),
            _q(report.direction),
            _q(calibrated),
            _q(rating.get("quadrant", "")),
            _q(vetoes),
            _ts(report.start),
            _ts(report.end),
            f"'{stamp}'",
        ]
    )
    _exec(f"INSERT INTO factor_scorecard VALUES ({values})")
    logger.info("scorecard: %s %s %.1f", report.factor_id, rating.get("grade"), rating.get("score", 0))


__all__ = ["DDL", "ensure_table", "save"]
