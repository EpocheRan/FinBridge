"""构建/重建状态机。

``BuildState`` 是 domain 对象，repository 只负责持久化与读取，
不负责状态迁移。所有阶段推进都通过本模块的方法完成。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from .errors import CoverageIncomplete, ScreenerError
from screener.domain.clock import now_local


class BuildKind(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    JOB = "JOB"


class BuildStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BuildQuality(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class BuildPhase(str, Enum):
    PLAN = "PLAN"
    RAW = "RAW"
    REFERENCE = "REFERENCE"
    COMPUTE = "COMPUTE"
    WRITE = "WRITE"
    VERIFY = "VERIFY"
    DONE = "DONE"


_TERMINAL = {
    BuildStatus.DONE,
    BuildStatus.PARTIAL,
    BuildStatus.FAILED,
    BuildStatus.CANCELLED,
}


def make_build_id(
    *,
    kind: BuildKind | str,
    dataset: str,
    scope_key: str,
    universe_key: str,
    freq: str,
    formula_version: str,
) -> str:
    kind_value = kind.value if isinstance(kind, BuildKind) else str(kind)
    raw = "|".join(
        [kind_value, dataset, scope_key, universe_key, freq, formula_version]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@dataclass
class BuildState:
    build_id: str
    build_kind: BuildKind
    dataset: str
    scope: str
    scope_key: str
    universe_key: str
    universe_payload: str = ""
    freq: str = "d"
    formula_version: str = "daily-v1"

    window_start: date | None = None
    window_end: date | None = None

    status: BuildStatus = BuildStatus.PENDING
    quality: BuildQuality = BuildQuality.UNKNOWN
    phase: BuildPhase = BuildPhase.PLAN

    cursor_ts: datetime | None = None
    cursor_code: str | None = None

    requested_codes: int = 0
    built_codes: int = 0
    coverage_ratio: float = 0.0

    progress_current: int = 0
    progress_total: int = 0
    rows_written: int = 0

    attempts: int = 0
    max_attempts: int = 3
    message: str | None = None
    last_error: str | None = None
    retry_at: datetime | None = None

    locked_by: str | None = None
    locked_until: datetime | None = None

    created_at: datetime = field(default_factory=now_local)
    updated_at: datetime = field(default_factory=now_local)
    finished_at: datetime | None = None

    # ------------------------------------------------------------------ 工厂
    @classmethod
    def new_snapshot(
        cls,
        *,
        dataset: str,
        as_of: date,
        universe_key: str,
        universe_payload: str,
        freq: str,
        formula_version: str,
        requested_codes: int,
        max_attempts: int = 3,
    ) -> "BuildState":
        build_id = make_build_id(
            kind=BuildKind.SNAPSHOT,
            dataset=dataset,
            scope_key=as_of.isoformat(),
            universe_key=universe_key,
            freq=freq,
            formula_version=formula_version,
        )
        now = now_local()
        return cls(
            build_id=build_id,
            build_kind=BuildKind.SNAPSHOT,
            dataset=dataset,
            scope="date",
            scope_key=as_of.isoformat(),
            universe_key=universe_key,
            universe_payload=universe_payload,
            freq=freq,
            formula_version=formula_version,
            window_start=as_of,
            window_end=as_of,
            requested_codes=requested_codes,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def new_job(
        cls,
        *,
        dataset: str,
        start: date,
        end: date,
        universe_key: str,
        universe_payload: str,
        freq: str,
        formula_version: str,
        requested_codes: int,
        max_attempts: int = 3,
    ) -> "BuildState":
        scope_key = f"{start.isoformat()}:{end.isoformat()}"
        build_id = make_build_id(
            kind=BuildKind.JOB,
            dataset=dataset,
            scope_key=scope_key,
            universe_key=universe_key,
            freq=freq,
            formula_version=formula_version,
        )
        now = now_local()
        return cls(
            build_id=build_id,
            build_kind=BuildKind.JOB,
            dataset=dataset,
            scope="job",
            scope_key=scope_key,
            universe_key=universe_key,
            universe_payload=universe_payload,
            freq=freq,
            formula_version=formula_version,
            window_start=start,
            window_end=end,
            requested_codes=requested_codes,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------ 判断
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def is_hit(self, formula_version: str | None = None) -> bool:
        if self.status != BuildStatus.DONE or self.quality != BuildQuality.FULL:
            return False
        return formula_version is None or self.formula_version == formula_version

    def is_partial_hit(self, formula_version: str | None = None) -> bool:
        if self.status != BuildStatus.PARTIAL:
            return False
        return formula_version is None or self.formula_version == formula_version

    def lease_active(self, now: datetime | None = None) -> bool:
        now = now or now_local()
        return bool(
            self.status == BuildStatus.RUNNING
            and self.locked_until
            and self.locked_until > now
        )

    def can_resume(self, now: datetime | None = None) -> bool:
        now = now or now_local()
        if self.status == BuildStatus.PENDING:
            return True
        if self.status == BuildStatus.PARTIAL:
            return self.retry_at is not None and self.retry_at <= now
        if self.status == BuildStatus.FAILED:
            return self.retry_at is None or self.retry_at <= now
        return self.status == BuildStatus.RUNNING and not self.lease_active(now)

    # ------------------------------------------------------------------ 迁移
    def claim(
        self,
        owner: str,
        ttl_seconds: int = 300,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> bool:
        now = now or now_local()
        if self.lease_active(now) and self.locked_by != owner:
            return False
        if self.status == BuildStatus.DONE and not force:
            return False
        if self.status == BuildStatus.PARTIAL and not force:
            return False
        if self.attempts >= self.max_attempts and not force:
            return False

        self.status = BuildStatus.RUNNING
        self.quality = BuildQuality.UNKNOWN
        self.phase = BuildPhase.PLAN
        self.attempts += 1
        self.locked_by = owner
        self.locked_until = now + timedelta(seconds=ttl_seconds)
        self.updated_at = now
        self.finished_at = None
        if force:
            self.progress_current = 0
            self.rows_written = 0
        return True

    def heartbeat(
        self, owner: str, ttl_seconds: int = 300, now: datetime | None = None
    ) -> None:
        now = now or now_local()
        if self.locked_by == owner:
            self.locked_until = now + timedelta(seconds=ttl_seconds)
            self.updated_at = now

    def begin_phase(self, phase: BuildPhase, now: datetime | None = None) -> None:
        self.phase = phase
        self.updated_at = now or now_local()

    def advance(
        self,
        *,
        cursor_ts: datetime | None = None,
        cursor_code: str | None = None,
        processed: int = 1,
        total: int | None = None,
        rows_written: int = 0,
        now: datetime | None = None,
    ) -> None:
        if cursor_ts is not None:
            self.cursor_ts = cursor_ts
        if cursor_code is not None:
            self.cursor_code = cursor_code
        self.progress_current += max(0, int(processed))
        if total is not None:
            self.progress_total = max(self.progress_total, int(total))
        self.rows_written += max(0, int(rows_written))
        self.updated_at = now or now_local()

    def mark_done(
        self,
        *,
        coverage_ratio: float = 1.0,
        built_codes: int | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or now_local()
        self.status = BuildStatus.DONE
        self.quality = BuildQuality.FULL
        self.phase = BuildPhase.DONE
        self.coverage_ratio = min(1.0, max(0.0, coverage_ratio))
        self.built_codes = (
            self.requested_codes if built_codes is None else int(built_codes)
        )
        self.progress_current = max(self.progress_current, self.progress_total)
        self.locked_by = None
        self.locked_until = None
        self.retry_at = None
        self.message = None
        self.finished_at = now
        self.updated_at = now

    def mark_partial(
        self,
        reason: str,
        *,
        coverage_ratio: float = 0.0,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or now_local()
        self.status = BuildStatus.PARTIAL
        self.quality = BuildQuality.PARTIAL
        self.phase = BuildPhase.DONE
        self.coverage_ratio = min(1.0, max(0.0, coverage_ratio))
        self.message = reason
        self.retry_at = retry_at
        self.locked_by = None
        self.locked_until = None
        self.finished_at = now
        self.updated_at = now

    def mark_failed(
        self,
        error: str,
        *,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or now_local()
        self.status = BuildStatus.FAILED
        self.quality = BuildQuality.UNKNOWN
        self.phase = BuildPhase.DONE
        self.last_error = error
        self.retry_at = retry_at
        self.locked_by = None
        self.locked_until = None
        self.finished_at = now
        self.updated_at = now

    def release(self, owner: str, now: datetime | None = None) -> None:
        if self.locked_by == owner:
            self.locked_by = None
            self.locked_until = None
            self.updated_at = now or now_local()

    def require_resumable(self) -> None:
        if not self.is_terminal() and not self.can_resume():
            raise ScreenerError(
                f"build {self.build_id} 当前不可恢复: {self.status.value}"
            )

    def assert_complete(self) -> None:
        if self.status != BuildStatus.DONE:
            raise CoverageIncomplete(
                f"build {self.build_id} 未完成: {self.status.value}",
                build_id=self.build_id,
                status=self.status.value,
            )
