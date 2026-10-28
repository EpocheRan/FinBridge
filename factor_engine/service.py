"""因子计算引擎服务：注册内置因子、计算、写库、ensure。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, timedelta
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.timeutil import now_cn
from factors.registry import register_factor, get as get_spec
from factors.spec import FactorSpec

from .data import PriceData
from .definitions import (
    LEGACY_COLUMNS,
    LEGACY_SPECS,
    PRICE_FACTORS,
    LegacyComputer,
    builtin_map,
)
from .store import FactorStore
from .versions import price_source_version

logger = logging.getLogger("FactorEngine.Service")


class FactorEngineService:
    def __init__(
        self,
        *,
        store: FactorStore | None = None,
        data: PriceData | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        self.store = store or FactorStore(config=self.config)
        self.data = data or PriceData(config=self.config)
        self.builtins = builtin_map()
        self.legacy_columns = dict(LEGACY_COLUMNS)
        self.legacy = LegacyComputer(self.data, self.config)
        self.all_ids = [spec.factor_id for spec in self.list_factors()]
        self.register_builtins()
        self._registry_synced = False

    def sync_registry(self) -> int:
        """把内置因子写入 factor_registry / factor_alias（幂等）。"""
        specs = self.list_factors()
        written = 0
        if hasattr(self.store, "upsert_registry"):
            written = self.store.upsert_registry(specs)
        aliases = [
            (alias, spec.factor_id, spec.window)
            for spec in specs
            for alias in spec.aliases
        ]
        if aliases and hasattr(self.store, "upsert_aliases"):
            self.store.upsert_aliases(aliases)
        return written

    def _ensure_registry(self) -> None:
        if self._registry_synced:
            return
        try:
            self.sync_registry()
        except Exception as exc:
            logger.warning("同步 factor_registry 失败: %s", exc)
        self._registry_synced = True

    def register_builtins(self) -> list[str]:
        registered: list[str] = []
        for item in PRICE_FACTORS:
            register_factor(item.spec, replace=True)
            registered.append(item.spec.factor_id)
        for spec in LEGACY_SPECS:
            register_factor(spec, replace=True)
            registered.append(spec.factor_id)
        return sorted(registered)

    def list_factors(self) -> list[FactorSpec]:
        return [item.spec for item in PRICE_FACTORS] + list(LEGACY_SPECS)

    # ------------------------------------------------------------------ build
    def build(
        self,
        factor_ids: Sequence[str] | None,
        start_date: str | date,
        end_date: str | date,
        codes: Sequence[str] | None = None,
        *,
        write_latest: bool = True,
    ) -> dict[str, Any]:
        ids = [str(item) for item in (factor_ids or self.all_ids)]
        specs = [get_spec(item) for item in ids]
        legacy_specs = [
            spec for spec in specs if spec.factor_id in self.legacy_columns
        ]
        compute_specs = [
            spec for spec in specs if spec.factor_id not in self.legacy_columns
        ]
        self._ensure_registry()
        # 带 warmup 加载，避免短区间算不出长窗因子；写库时再裁回请求区间
        warmup = max((spec.warmup for spec in specs), default=0)
        if legacy_specs:
            warmup = max(warmup, 300)  # 52 周高低点/252 日 beta
        load_start = pd.Timestamp(start_date).normalize() - pd.Timedelta(
            days=max(0, warmup) * 2 + 10
        )
        start_ts = pd.Timestamp(start_date).normalize()
        frame = self.data.load(codes, load_start, end_date)
        if frame is None or frame.empty:
            return {
                "job_id": None,
                "rows_written": 0,
                "factor_ids": ids,
                "reason": "empty price data",
            }

        source_version = price_source_version(frame)
        computed_at = now_cn()
        records: list[dict[str, Any]] = []

        if legacy_specs:
            selected_codes = (
                list(codes)
                if codes
                else sorted(frame["code"].astype(str).unique())
            )
            wide = self.legacy.compute(
                frame, selected_codes, load_start, end_date
            )
            for spec in legacy_specs:
                column = self.legacy_columns[spec.factor_id]
                if wide.empty or column not in wide.columns:
                    logger.warning("legacy 列缺失: %s.%s", spec.factor_id, column)
                    continue
                part = wide[["ts", "code", column]].dropna(subset=[column])
                for row in part.itertuples(index=False):
                    stamp = pd.Timestamp(row.ts).normalize()
                    if stamp < start_ts:
                        continue
                    records.append(
                        {
                            "ts": stamp,
                            "code": str(row.code),
                            "factor_id": spec.factor_id,
                            "value": float(row[2]),
                            "quality": "ok",
                            "source": spec.source.dataset,
                            "source_version": source_version,
                            "version": spec.version,
                            "computed_at": computed_at,
                        }
                    )

        for spec in compute_specs:
            builtin = self.builtins.get(spec.factor_id)
            if builtin is None:
                logger.warning("跳过未实现计算的内置因子: %s", spec.factor_id)
                continue
            for code, group in frame.groupby("code", sort=True):
                group = group.sort_values("ts")
                values = builtin.compute(group)
                for ts, value in zip(group["ts"], values):
                    if pd.isna(value):
                        continue
                    stamp = pd.Timestamp(ts).normalize()
                    if stamp < start_ts:
                        continue
                    records.append(
                        {
                            "ts": stamp,
                            "code": str(code),
                            "factor_id": spec.factor_id,
                            "value": float(value),
                            "quality": "ok",
                            "source": spec.source.dataset,
                            "source_version": source_version,
                            "version": spec.version,
                            "computed_at": computed_at,
                        }
                    )

        if not records:
            return {
                "job_id": None,
                "rows_written": 0,
                "factor_ids": ids,
                "source_version": source_version,
            }

        values = pd.DataFrame(records)
        written = self.store.upsert_values(values)
        if write_latest:
            self.store.upsert_latest(values)
        job_id = f"build:{uuid.uuid4().hex[:12]}"
        self.store.upsert_build_state(
            {
                "job_id": job_id,
                "dataset": "ohlcv_1d",
                "factor_ids": ",".join(ids),
                "window_start": pd.Timestamp(start_date).normalize().to_pydatetime(),
                "window_end": pd.Timestamp(end_date).normalize().to_pydatetime(),
                "cursor": pd.Timestamp(end_date).normalize().to_pydatetime(),
                "status": "DONE",
                "attempts": 1,
                "rows_written": written,
                "payload": json.dumps(
                    {
                        "codes": list(codes or []),
                        "source_version": source_version,
                        "rows": written,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return {
            "job_id": job_id,
            "rows_written": written,
            "factor_ids": ids,
            "source_version": source_version,
            "codes": len(codes) if codes else None,
        }

    # ------------------------------------------------------------------ ensure
    def ensure(
        self,
        codes: Sequence[str],
        as_of: str | date,
        factor_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        ids = [str(item) for item in (factor_ids or self.all_ids)]
        target = pd.Timestamp(as_of).normalize()
        missing: list[str] = []
        for factor_id in ids:
            latest = self.store.latest_ts(factor_id)
            if latest is None or latest < target:
                missing.append(factor_id)
        if not missing:
            return {"built": False, "factor_ids": ids, "as_of": str(target.date())}
        result = self.build(missing, target, target, codes)
        result.update({"built": True, "as_of": str(target.date())})
        return result

    # ------------------------------------------------------------------ read
    def read(
        self,
        factor_ids: Sequence[str],
        start_date: str | date,
        end_date: str | date,
        codes: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        return self.store.query_values(factor_ids, start_date, end_date, codes)

    def read_latest(
        self, factor_ids: Sequence[str], codes: Sequence[str] | None = None
    ) -> pd.DataFrame:
        return self.store.query_latest(factor_ids, codes)
