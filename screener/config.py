"""Screener 配置对象。

只由 bootstrap 读取 ``config.json``；domain / application 接收配置值对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from screener.domain.baseline import BaselineConfig
from screener.domain.factor_engine import DailyFactorConfig


@dataclass
class BackfillSettings:
    max_lookback_bars: int = 260
    max_sync_codes: int = 300
    max_sync_days: int = 750
    sync_budget_ms: int = 2000
    code_chunk_size: int = 200
    daily_chunk_years: int = 2
    minute_chunk_days: int = 5
    source_priority: list[str] = field(default_factory=lambda: ["baostock", "akshare"])
    intraday_source_priority: list[str] = field(default_factory=lambda: ["akshare"])
    negative_cache_ttl_hours: int = 24
    max_retries: int = 3
    lock_ttl_seconds: int = 300
    fetch_missing_reference: bool = False
    max_reference_fetch: int = 20
    fetch_benchmark: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "BackfillSettings":
        payload = payload or {}
        return cls(
            max_lookback_bars=int(payload.get("max_lookback_bars", 260)),
            max_sync_codes=int(payload.get("max_sync_codes", 300)),
            max_sync_days=int(payload.get("max_sync_days", 750)),
            sync_budget_ms=int(payload.get("sync_budget_ms", 2000)),
            code_chunk_size=int(payload.get("code_chunk_size", 200)),
            daily_chunk_years=int(payload.get("daily_chunk_years", 2)),
            minute_chunk_days=int(payload.get("minute_chunk_days", 5)),
            source_priority=list(
                payload.get("source_priority", ["baostock", "akshare"])
            ),
            intraday_source_priority=list(
                payload.get("intraday_source_priority", ["akshare"])
            ),
            negative_cache_ttl_hours=int(
                payload.get("negative_cache_ttl_hours", 24)
            ),
            max_retries=int(payload.get("max_retries", 3)),
            lock_ttl_seconds=int(payload.get("lock_ttl_seconds", 300)),
            fetch_missing_reference=bool(
                payload.get("fetch_missing_reference", False)
            ),
            max_reference_fetch=int(payload.get("max_reference_fetch", 20)),
            fetch_benchmark=bool(payload.get("fetch_benchmark", False)),
        )


@dataclass
class WorkerSettings:
    max_workers: int = 2
    progress_interval: int = 50
    start_on_bootstrap: bool = False
    queue_size: int = 100

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "WorkerSettings":
        payload = payload or {}
        return cls(
            max_workers=max(1, int(payload.get("max_workers", 2))),
            progress_interval=max(1, int(payload.get("progress_interval", 50))),
            start_on_bootstrap=bool(payload.get("start_on_bootstrap", False)),
            queue_size=max(1, int(payload.get("queue_size", 100))),
        )


@dataclass
class QuerySettings:
    default_limit: int = 100
    max_limit: int = 1000

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "QuerySettings":
        payload = payload or {}
        return cls(
            default_limit=int(payload.get("default_limit", 100)),
            max_limit=int(payload.get("max_limit", 1000)),
        )


@dataclass
class ScreenerSettings:
    enabled: bool = True
    benchmark_code: str = "000300"
    benchmark_market: str = "IDX"
    daily: DailyFactorConfig = field(default_factory=DailyFactorConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    backfill: BackfillSettings = field(default_factory=BackfillSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    query: QuerySettings = field(default_factory=QuerySettings)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ScreenerSettings":
        payload = payload or {}
        benchmark = payload.get("benchmark") or {}
        benchmark_code = str(benchmark.get("code", "000300"))
        benchmark_market = str(benchmark.get("market", "IDX"))

        daily_payload = dict(payload.get("daily") or {})
        daily_payload.setdefault("benchmark_code", benchmark_code)
        daily_payload.setdefault("benchmark_market", benchmark_market)

        return cls(
            enabled=bool(payload.get("enabled", True)),
            benchmark_code=benchmark_code,
            benchmark_market=benchmark_market,
            daily=DailyFactorConfig.from_dict(daily_payload),
            baseline=BaselineConfig.from_dict(payload.get("baseline")),
            backfill=BackfillSettings.from_dict(payload.get("backfill")),
            worker=WorkerSettings.from_dict(payload.get("worker")),
            query=QuerySettings.from_dict(payload.get("query")),
        )
