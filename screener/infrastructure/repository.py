"""QuestDB 仓储实现（FactorStorePort / BuildStatePort / SuspensionPort）。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Iterable, Sequence

import pandas as pd

from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import instrument_market
from screener.domain.clock import now_local
from screener.domain.build_state import BuildState
from screener.domain.suspension import Suspension, SuspensionStatus
from screener.infrastructure.mappers import (
    BASELINE_DB_COLUMNS,
    BUILD_STATE_COLUMNS,
    SUSPENSION_COLUMNS,
    baseline_to_rows,
    build_state_to_row,
    row_to_build_state,
    row_to_suspension,
    suspension_to_row,
)

logger = logging.getLogger("Screener.Repository")


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def _start_ts(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time())


def _end_ts(value: date) -> datetime:
    return datetime.combine(value, datetime.max.time()).replace(microsecond=0)


class QuestDBFactorRepository:
    def __init__(self, db: QuestDBClient):
        self.db = db

    # ------------------------------------------------------------------ daily
    def read_daily(
        self, codes: Sequence[str], start: date, end: date
    ) -> pd.DataFrame:
        codes = [str(code).split(".")[0] for code in codes]
        if not codes:
            return pd.DataFrame()
        sql = (
            'SELECT * FROM "factor_daily_wide" '
            "WHERE ts BETWEEN %s AND %s "
            f"AND code IN ({_placeholders(len(codes))}) "
            "ORDER BY ts, code"
        )
        params = [_start_ts(start), _end_ts(end), *codes]
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return frame

    def coverage(
        self, codes: Sequence[str], start: date, end: date
    ) -> pd.DataFrame:
        codes = [str(code).split(".")[0] for code in codes]
        if not codes:
            return pd.DataFrame()
        sql = (
            'SELECT ts, code, formula_version, is_partial, full_ready '
            'FROM "factor_daily_wide" '
            "WHERE ts BETWEEN %s AND %s "
            f"AND code IN ({_placeholders(len(codes))}) "
            "ORDER BY code, ts"
        )
        params = [_start_ts(start), _end_ts(end), *codes]
        frame = self.db.query_dataframe(sql, params)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return frame

    def has_rows(self, codes: Sequence[str], as_of: date) -> bool:
        codes = [str(code).split(".")[0] for code in codes]
        if not codes:
            return False
        sql = (
            "SELECT count_distinct(code) FROM \"factor_daily_wide\" "
            "WHERE ts = %s "
            f"AND code IN ({_placeholders(len(codes))})"
        )
        row = self.db.query(
            sql, [datetime.combine(as_of, datetime.min.time()), *codes], fetch_all=False
        )
        return bool(row and row[0])

    def count_rows(self, codes: Sequence[str], as_of: date) -> int:
        codes = [str(code).split(".")[0] for code in codes]
        if not codes:
            return 0
        sql = (
            "SELECT count_distinct(code) FROM \"factor_daily_wide\" "
            "WHERE ts = %s "
            f"AND code IN ({_placeholders(len(codes))})"
        )
        row = self.db.query(
            sql, [datetime.combine(as_of, datetime.min.time()), *codes], fetch_all=False
        )
        return int(row[0]) if row else 0

    def wait_daily_visible(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        timeout: float = 3.0,
    ) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            if self.count_rows(codes, end) > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def upsert_daily(self, frame: pd.DataFrame) -> int:
        # 写实现归 factor_engine；screener 只做构建编排
        from factor_engine.wide_store import upsert_wide

        return upsert_wide(self.db, frame)

    # ------------------------------------------------------------------ baseline
    def latest_baseline_version(self, code: str, freq: str) -> date | None:
        sql = (
            'SELECT max(as_of_date) FROM "intraday_baseline" '
            "WHERE code = %s AND freq = %s"
        )
        row = self.db.query(sql, (str(code), str(freq)), fetch_all=False)
        if not row or row[0] is None:
            return None
        value = row[0]
        return value.date() if hasattr(value, "date") else date.fromisoformat(str(value)[:10])

    def read_baseline(self, code: str, version: date, freq: str) -> pd.DataFrame:
        sql = (
            'SELECT * FROM "intraday_baseline" '
            "WHERE code = %s AND freq = %s AND as_of_date = %s "
            "ORDER BY minute_end"
        )
        frame = self.db.query_dataframe(
            sql,
            (
                str(code),
                str(freq),
                datetime.combine(version, datetime.min.time()),
            ),
        )
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return frame

    def upsert_baseline(self, frame: pd.DataFrame) -> int:
        rows = baseline_to_rows(frame)
        if not rows:
            return 0
        columns = ", ".join(f'"{column}"' for column in BASELINE_DB_COLUMNS)
        sql = f'INSERT INTO "intraday_baseline" ({columns}) VALUES %s'
        return self.db.execute_values(sql, rows, page_size=500)

    def wait_baseline_visible(
        self, code: str, version: date, freq: str, timeout: float = 3.0
    ) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            latest = self.latest_baseline_version(code, freq)
            if latest is not None and latest >= version:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


class QuestDBBuildStateRepository:
    def __init__(self, db: QuestDBClient):
        self.db = db
        self._lock = threading.RLock()

    def get_latest(self, build_id: str) -> BuildState | None:
        sql = (
            'SELECT * FROM "screener_build_state" '
            "WHERE build_id = %s "
            "LATEST ON updated_at PARTITION BY build_id"
        )
        frame = self.db.query_dataframe(sql, (str(build_id),))
        if frame is None or frame.empty:
            return None
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return row_to_build_state(frame.iloc[0])

    def save(self, state: BuildState) -> None:
        columns = ", ".join(f'"{column}"' for column in BUILD_STATE_COLUMNS)
        sql = f'INSERT INTO "screener_build_state" ({columns}) VALUES ({_placeholders(len(BUILD_STATE_COLUMNS))})'
        self.db.execute(sql, build_state_to_row(state))

    def try_claim(
        self,
        build_id: str,
        owner: str,
        ttl_seconds: int = 300,
        *,
        force: bool = False,
    ) -> BuildState | None:
        with self._lock:
            state = self.get_latest(build_id)
            # QuestDB WAL apply 可能比 INSERT 稍慢；刚 save 完立即 claim 时
            # 给一个很短的可见性重试窗口，避免第一次读请求误返回 PENDING。
            if state is None:
                deadline = time.monotonic() + 2.0
                while state is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                    state = self.get_latest(build_id)
            if state is None:
                return None
            if not state.claim(owner, ttl_seconds, force=force):
                return None
            self.save(state)
            return state

    def list_recoverable(self, now: datetime | None = None) -> list[BuildState]:
        now = now or now_local()
        sql = (
            'SELECT * FROM "screener_build_state" '
            "LATEST ON updated_at PARTITION BY build_id"
        )
        frame = self.db.query_dataframe(sql)
        if frame is None or frame.empty:
            return []
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        result: list[BuildState] = []
        for _, row in frame.iterrows():
            state = row_to_build_state(row)
            if state.can_resume(now):
                result.append(state)
        return result

    def list_recent(self, limit: int = 20) -> list[BuildState]:
        sql = (
            'SELECT * FROM "screener_build_state" '
            "LATEST ON updated_at PARTITION BY build_id "
            "ORDER BY updated_at DESC "
            f"LIMIT {max(1, int(limit))}"
        )
        frame = self.db.query_dataframe(sql)
        if frame is None or frame.empty:
            return []
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        return [row_to_build_state(row) for _, row in frame.iterrows()]


class QuestDBSuspensionRepository:
    def __init__(self, db: QuestDBClient):
        self.db = db

    def lookup(
        self, code: str, day: date, market: str = "MLCN"
    ) -> Suspension | None:
        records = self.lookup_range(code, day, day, market=market)
        if not records:
            return None
        return max(
            records,
            key=lambda item: {
                SuspensionStatus.SUSPENDED: 2,
                SuspensionStatus.DELISTED: 3,
                SuspensionStatus.NO_DATA: 1,
            }.get(item.status, 0),
        )

    def lookup_range(
        self, code: str, start: date, end: date, market: str = "MLCN"
    ) -> list[Suspension]:
        return self.lookup_many([code], start, end, market=market).get(str(code), [])

    def lookup_many(
        self,
        codes: Sequence[str],
        start: date,
        end: date,
        market: str = "MLCN",
    ) -> dict[str, list[Suspension]]:
        codes = [str(code).split(".")[0] for code in codes]
        if not codes:
            return {}
        placeholders = ", ".join(["%s"] * len(codes))
        sql = (
            'SELECT * FROM "market_suspension" '
            f"WHERE code IN ({placeholders}) "
            "LATEST ON updated_at PARTITION BY code, market, status"
        )
        frame = self.db.query_dataframe(sql, list(codes))
        if frame is None or frame.empty:
            return {}
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        result: dict[str, list[Suspension]] = {}
        for _, row in frame.iterrows():
            item = row_to_suspension(row)
            overlaps = item.start_date <= end and (
                item.end_date is None or item.end_date >= start
            )
            if not overlaps:
                continue
            if item.status == SuspensionStatus.NO_DATA:
                if item.is_retry_blocked():
                    result.setdefault(item.code, []).append(item)
                continue
            result.setdefault(item.code, []).append(item)
        return result

    def upsert(self, items: Iterable[Suspension]) -> int:
        rows = [suspension_to_row(item) for item in items]
        if not rows:
            return 0
        # market 统一落交易所（旧数据里的 MLCN 已迁移）
        code_i = SUSPENSION_COLUMNS.index("code")
        market_i = SUSPENSION_COLUMNS.index("market")
        rows = [
            tuple(instrument_market(row[code_i]) if i == market_i else value
                  for i, value in enumerate(row))
            for row in rows
        ]
        columns = ", ".join(f'"{column}"' for column in SUSPENSION_COLUMNS)
        sql = f'INSERT INTO "market_suspension" ({columns}) VALUES %s'
        return self.db.execute_values(sql, rows, page_size=200)
