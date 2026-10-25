"""Screener 领域值对象。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Sequence

from .errors import InvalidScreenSpec


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and not isinstance(value, str):
        try:
            return value.date()
        except Exception:
            pass
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception as exc:
        raise InvalidScreenSpec(f"无法解析日期: {value}") from exc


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def as_tuple(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()

    def __contains__(self, day: date) -> bool:
        return self.start <= day <= self.end


class GapType(str, Enum):
    RAW_MISSING = "RAW_MISSING"
    FACTOR_MISSING = "FACTOR_MISSING"
    FACTOR_STALE = "FACTOR_STALE"
    LOOKBACK_SHORT = "LOOKBACK_SHORT"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


@dataclass
class Gap:
    code: str
    dates: list[date]
    gap_type: GapType
    reason: str | None = None

    def ranges(self) -> list[DateRange]:
        return merge_date_ranges(self.dates)


@dataclass
class CoverageReport:
    dataset: str
    requested_codes: int = 0
    available_codes: int = 0
    requested_start: date | None = None
    requested_end: date | None = None
    missing_ranges: dict[str, list[DateRange]] = field(default_factory=dict)
    partial_codes: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    complete: bool = False

    @property
    def coverage_ratio(self) -> float:
        if not self.requested_codes:
            return 1.0
        return min(1.0, max(0.0, self.available_codes / self.requested_codes))

    @property
    def missing_codes(self) -> list[str]:
        return sorted(self.missing_ranges)

    def merge(self, other: "CoverageReport") -> "CoverageReport":
        missing = {key: list(value) for key, value in self.missing_ranges.items()}
        for key, value in other.missing_ranges.items():
            missing.setdefault(key, []).extend(value)
        return CoverageReport(
            dataset=self.dataset,
            requested_codes=self.requested_codes + other.requested_codes,
            available_codes=self.available_codes + other.available_codes,
            requested_start=min(
                [d for d in (self.requested_start, other.requested_start) if d],
                default=None,
            ),
            requested_end=max(
                [d for d in (self.requested_end, other.requested_end) if d],
                default=None,
            ),
            missing_ranges=missing,
            partial_codes=sorted(set(self.partial_codes) | set(other.partial_codes)),
            errors={**self.errors, **other.errors},
            complete=self.complete and other.complete,
        )

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "requested_codes": self.requested_codes,
            "available_codes": self.available_codes,
            "coverage_ratio": round(self.coverage_ratio, 6),
            "requested_start": self.requested_start.isoformat()
            if self.requested_start
            else None,
            "requested_end": self.requested_end.isoformat()
            if self.requested_end
            else None,
            "missing_ranges": {
                code: [
                    {"start": item.start.isoformat(), "end": item.end.isoformat()}
                    for item in ranges
                ]
                for code, ranges in self.missing_ranges.items()
            },
            "partial_codes": self.partial_codes,
            "errors": self.errors,
            "complete": self.complete,
        }


@dataclass
class UniverseSpec:
    market: str = "MLCN"
    codes: list[str] | None = None
    exclude_st: bool = True
    boards: list[str] | None = None
    all_market: bool = False

    def normalized_codes(self) -> list[str] | None:
        if self.codes is None:
            return None
        return sorted({str(code).split(".")[0] for code in self.codes if str(code)})

    @property
    def universe_key(self) -> str:
        if self.codes is None or self.all_market:
            return "ALL"
        payload = ",".join(self.normalized_codes() or [])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def key_for(self, codes: Sequence[str] | None = None) -> str:
        """带代码清单指纹的 universe key。

        全市场场景下 ``universe_key`` 恒为 ``"ALL"``，新增上市/退市不会改变
        build_id，导致快照永远命中旧结果。这里把**实际解析出的代码清单**哈希
        进去：清单一变 build_id 就变，会自动重建。
        """
        source = codes if codes is not None else (self.codes or [])
        normalized = sorted(
            {str(code).split(".")[0] for code in source if str(code)}
        )
        digest = hashlib.sha1(",".join(normalized).encode("utf-8")).hexdigest()[:16]
        prefix = "ALL" if (self.all_market or self.codes is None) else "CODES"
        return f"{prefix}:{self.market}:{digest}"

    @property
    def payload(self) -> str:
        if self.codes is None or self.all_market:
            return ""
        return json.dumps(self.normalized_codes() or [], ensure_ascii=False)


@dataclass
class Filter:
    field: str
    op: str
    value: Any = None


@dataclass
class Sort:
    field: str
    desc: bool = True


@dataclass
class ScreenSpec:
    as_of: date | None = None
    universe: UniverseSpec = field(default_factory=UniverseSpec)
    filters: list[Filter] = field(default_factory=list)
    sort: list[Sort] = field(default_factory=list)
    #: None = 使用配置里的 query.default_limit；上限由 ScreenerService 按配置校验
    limit: int | None = None
    offset: int = 0
    auto_build: bool = True
    allow_partial: bool = False
    budget_ms: int = 5000

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ScreenSpec":
        payload = payload or {}
        universe_payload = payload.get("universe") or {}
        raw_codes = universe_payload.get("codes")
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        raw_boards = universe_payload.get("boards")
        if isinstance(raw_boards, str):
            raw_boards = [raw_boards]
        filters = [
            Filter(
                field=str(item.get("field", "")),
                op=str(item.get("op", "eq")).lower(),
                value=item.get("value"),
            )
            for item in payload.get("filters", []) or []
        ]
        sorts = [
            Sort(field=str(item.get("field", "")), desc=bool(item.get("desc", True)))
            for item in payload.get("sort", []) or []
        ]
        spec = cls(
            as_of=_parse_date(payload.get("as_of")),
            universe=UniverseSpec(
                market=str(universe_payload.get("market", "MLCN")),
                codes=raw_codes,
                exclude_st=bool(universe_payload.get("exclude_st", True)),
                boards=raw_boards,
                all_market=bool(universe_payload.get("all_market", False)),
            ),
            filters=filters,
            sort=sorts,
            limit=int(payload["limit"]) if payload.get("limit") is not None else None,
            offset=int(payload.get("offset", 0) or 0),
            auto_build=bool(payload.get("auto_build", True)),
            allow_partial=bool(payload.get("allow_partial", False)),
            budget_ms=int(payload.get("budget_ms", 5000) or 5000),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        from .fields import FIELD_REGISTRY, validate_operator

        if self.limit is not None and self.limit <= 0:
            raise InvalidScreenSpec("limit 必须为正数")
        if self.offset < 0:
            raise InvalidScreenSpec("offset 不能为负数")
        if self.budget_ms <= 0:
            raise InvalidScreenSpec("budget_ms 必须为正数")

        for item in self.filters:
            field = FIELD_REGISTRY.get(item.field)
            if field is None:
                raise InvalidScreenSpec(f"不允许筛选字段: {item.field}")
            if not field.filterable:
                raise InvalidScreenSpec(f"字段不可筛选: {item.field}")
            validate_operator(item.op, field, item.field)
            if item.op == "between" and (
                not isinstance(item.value, (list, tuple)) or len(item.value) != 2
            ):
                raise InvalidScreenSpec(f"between 需要两个值: {item.field}")
            if item.op == "in" and not isinstance(item.value, (list, tuple)):
                raise InvalidScreenSpec(f"in 需要列表: {item.field}")

        for item in self.sort:
            field = FIELD_REGISTRY.get(item.field)
            if field is None:
                raise InvalidScreenSpec(f"不允许排序字段: {item.field}")
            if not field.sortable:
                raise InvalidScreenSpec(f"字段不可排序: {item.field}")

        if self.universe.codes is not None:
            self.universe.codes = [
                str(code).split(".")[0]
                for code in self.universe.codes
                if str(code).strip()
            ]
            if not self.universe.codes:
                raise InvalidScreenSpec("universe.codes 不能为空列表")


@dataclass
class ScreenResult:
    as_of: date
    rows: list[dict[str, Any]]
    total: int = 0
    coverage: CoverageReport | None = None
    quality: str = "UNKNOWN"
    status: str = "UNKNOWN"
    build_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "rows": self.rows,
            "total": self.total,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "quality": self.quality,
            "status": self.status,
            "build_id": self.build_id,
            "warnings": self.warnings,
            "pending": self.pending,
        }


@dataclass
class BuildRequest:
    dataset: str = "screener_daily"
    start: date | None = None
    end: date | None = None
    codes: list[str] | None = None
    force: bool = False
    freq: str = "d"
    formula_version: str = "daily-v1"

    def __post_init__(self) -> None:
        if self.dataset not in ("screener_daily", "intraday_baseline"):
            raise InvalidScreenSpec(f"不支持的 dataset: {self.dataset}")
        if self.start is None or self.end is None:
            raise InvalidScreenSpec("BuildRequest 必须指定 start/end")
        if self.start > self.end:
            raise InvalidScreenSpec("start 不能晚于 end")

    @property
    def universe_key(self) -> str:
        if self.codes is None:
            return "ALL"
        payload = ",".join(sorted({str(code).split(".")[0] for code in self.codes}))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def universe_payload(self) -> str:
        if self.codes is None:
            return ""
        return json.dumps(
            sorted({str(code).split(".")[0] for code in self.codes}),
            ensure_ascii=False,
        )


def merge_date_ranges(dates: Sequence[date]) -> list[DateRange]:
    ordered = sorted(set(dates))
    if not ordered:
        return []
    ranges: list[DateRange] = []
    start = previous = ordered[0]
    for day in ordered[1:]:
        if day == previous + timedelta(days=1):
            previous = day
            continue
        ranges.append(DateRange(start, previous))
        start = previous = day
    ranges.append(DateRange(start, previous))
    return ranges
