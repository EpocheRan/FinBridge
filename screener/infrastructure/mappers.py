"""Domain 对象 / DataFrame 与 QuestDB 行之间的转换。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

from screener.domain.clock import now_local
from screener.domain.build_state import (
    BuildKind,
    BuildPhase,
    BuildQuality,
    BuildState,
    BuildStatus,
)
from screener.domain.factor_engine import DAILY_COLUMNS
from screener.domain.suspension import Suspension, SuspensionStatus

DAILY_DB_COLUMNS = [
    "ts",
    "code",
    "market",
    *[column for column in DAILY_COLUMNS if column not in ("ts", "code")],
    "formula_version",
    "computed_at",
]

BASELINE_DB_COLUMNS = [
    "as_of_date",
    "code",
    "market",
    "freq",
    "minute_end",
    "session",
    "avg_volume",
    "avg_cum_volume",
    "avg_amount",
    "avg_cum_amount",
    "std_cum_volume",
    "sample_days",
    "expected_days",
    "coverage_ratio",
    "includes_auction",
    "is_partial",
    "source_start",
    "source_end",
    "formula_version",
    "computed_at",
]

BUILD_STATE_COLUMNS = [
    "updated_at",
    "build_id",
    "build_kind",
    "dataset",
    "scope",
    "scope_key",
    "universe_key",
    "universe_payload",
    "freq",
    "formula_version",
    "window_start",
    "window_end",
    "status",
    "quality",
    "phase",
    "cursor_ts",
    "cursor_code",
    "requested_codes",
    "built_codes",
    "coverage_ratio",
    "progress_current",
    "progress_total",
    "rows_written",
    "attempts",
    "max_attempts",
    "message",
    "last_error",
    "retry_at",
    "locked_by",
    "locked_until",
    "created_at",
    "finished_at",
]

SUSPENSION_COLUMNS = [
    "updated_at",
    "code",
    "market",
    "status",
    "start_date",
    "end_date",
    "retry_at",
    "source",
    "reason",
    "created_at",
]


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return pd.Timestamp(value).to_pydatetime()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, np.generic):
        value = value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    return value


def _int_or_none(value: Any) -> int | None:
    value = clean_value(value)
    if value is None:
        return None
    return int(round(float(value)))


def daily_to_rows(frame: pd.DataFrame) -> list[tuple]:
    if frame is None or frame.empty:
        return []
    frame = frame.copy()
    for column in DAILY_DB_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    rows: list[tuple] = []
    for _, row in frame.iterrows():
        values: list[Any] = []
        for column in DAILY_DB_COLUMNS:
            value = clean_value(row[column])
            if column == "volume":
                value = _int_or_none(value)
            elif column in (
                "beta_window",
                "lookback_days",
            ):
                value = _int_or_none(value)
            elif column in (
                "is_new_high_52w",
                "is_new_low_52w",
                "is_partial",
                "core_ready",
                "full_ready",
            ):
                value = bool(value) if value is not None else None
            values.append(value)
        rows.append(tuple(values))
    return rows


def baseline_to_rows(frame: pd.DataFrame) -> list[tuple]:
    if frame is None or frame.empty:
        return []
    frame = frame.copy()
    for column in BASELINE_DB_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    rows: list[tuple] = []
    for _, row in frame.iterrows():
        values: list[Any] = []
        for column in BASELINE_DB_COLUMNS:
            value = clean_value(row[column])
            if column in (
                "minute_end",
                "sample_days",
                "expected_days",
            ):
                value = _int_or_none(value)
            elif column in ("includes_auction", "is_partial"):
                value = bool(value) if value is not None else None
            values.append(value)
        rows.append(tuple(values))
    return rows


def build_state_to_row(state: BuildState) -> tuple:
    values = {
        "updated_at": state.updated_at,
        "build_id": state.build_id,
        "build_kind": state.build_kind.value,
        "dataset": state.dataset,
        "scope": state.scope,
        "scope_key": state.scope_key,
        "universe_key": state.universe_key,
        "universe_payload": state.universe_payload or "",
        "freq": state.freq,
        "formula_version": state.formula_version,
        "window_start": state.window_start,
        "window_end": state.window_end,
        "status": state.status.value,
        "quality": state.quality.value,
        "phase": state.phase.value if isinstance(state.phase, BuildPhase) else str(state.phase),
        "cursor_ts": state.cursor_ts,
        "cursor_code": state.cursor_code,
        "requested_codes": state.requested_codes,
        "built_codes": state.built_codes,
        "coverage_ratio": state.coverage_ratio,
        "progress_current": state.progress_current,
        "progress_total": state.progress_total,
        "rows_written": state.rows_written,
        "attempts": state.attempts,
        "max_attempts": state.max_attempts,
        "message": state.message,
        "last_error": state.last_error,
        "retry_at": state.retry_at,
        "locked_by": state.locked_by,
        "locked_until": state.locked_until,
        "created_at": state.created_at,
        "finished_at": state.finished_at,
    }
    return tuple(clean_value(values[column]) for column in BUILD_STATE_COLUMNS)


def _as_datetime(value: Any) -> datetime | None:
    value = clean_value(value)
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime()


def _as_date(value: Any) -> date | None:
    parsed = _as_datetime(value)
    return parsed.date() if parsed else None


def row_to_build_state(row: Mapping[str, Any]) -> BuildState:
    def get(name: str, default: Any = None) -> Any:
        try:
            value = row.get(name, default)
        except AttributeError:
            value = default
        return clean_value(value)

    return BuildState(
        build_id=str(get("build_id")),
        build_kind=BuildKind(str(get("build_kind", "SNAPSHOT"))),
        dataset=str(get("dataset", "screener_daily")),
        scope=str(get("scope", "date")),
        scope_key=str(get("scope_key", "")),
        universe_key=str(get("universe_key", "ALL")),
        universe_payload=str(get("universe_payload") or ""),
        freq=str(get("freq", "d")),
        formula_version=str(get("formula_version", "daily-v1")),
        window_start=_as_date(get("window_start")),
        window_end=_as_date(get("window_end")),
        status=BuildStatus(str(get("status", "PENDING"))),
        quality=BuildQuality(str(get("quality", "UNKNOWN"))),
        phase=BuildPhase(str(get("phase", "PLAN"))),
        cursor_ts=_as_datetime(get("cursor_ts")),
        cursor_code=get("cursor_code"),
        requested_codes=int(get("requested_codes") or 0),
        built_codes=int(get("built_codes") or 0),
        coverage_ratio=float(get("coverage_ratio") or 0.0),
        progress_current=int(get("progress_current") or 0),
        progress_total=int(get("progress_total") or 0),
        rows_written=int(get("rows_written") or 0),
        attempts=int(get("attempts") or 0),
        max_attempts=int(get("max_attempts") or 3),
        message=get("message"),
        last_error=get("last_error"),
        retry_at=_as_datetime(get("retry_at")),
        locked_by=get("locked_by"),
        locked_until=_as_datetime(get("locked_until")),
        created_at=_as_datetime(get("created_at")) or now_local(),
        updated_at=_as_datetime(get("updated_at")) or now_local(),
        finished_at=_as_datetime(get("finished_at")),
    )


def suspension_to_row(item: Suspension) -> tuple:
    now = item.updated_at or now_local()
    values = {
        "updated_at": now,
        "code": item.code,
        "market": item.market,
        "status": item.status.value,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "retry_at": item.retry_at,
        "source": item.source,
        "reason": item.reason,
        "created_at": item.created_at or now,
    }
    return tuple(clean_value(values[column]) for column in SUSPENSION_COLUMNS)


def row_to_suspension(row: Mapping[str, Any]) -> Suspension:
    def get(name: str, default: Any = None) -> Any:
        try:
            return clean_value(row.get(name, default))
        except AttributeError:
            return default

    start = _as_date(get("start_date"))
    return Suspension(
        code=str(get("code")),
        market=str(get("market", "MLCN")),
        status=SuspensionStatus(str(get("status", "NO_DATA"))),
        start_date=start or date.today(),
        end_date=_as_date(get("end_date")),
        retry_at=_as_datetime(get("retry_at")),
        source=get("source"),
        reason=get("reason"),
        created_at=_as_datetime(get("created_at")),
        updated_at=_as_datetime(get("updated_at")),
    )
