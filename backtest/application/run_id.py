"""run_id 计算：同输入同 id，同 id 幂等。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

APP_VERSION = "0.4.0"


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, pd.Timestamp)):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if hasattr(value, "to_payload"):
        return value.to_payload()
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return repr(value)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def compute_run_id(
    *,
    loaded,
    spec,
    factors: Sequence[Any],
    universe,
    snapshot,
    benchmark: pd.Series | None = None,
    security_status=None,
    engine_name: str = "pybroker",
    engine_version: str = "none",
    seed: int = 0,
) -> str:
    strategy = loaded.strategy
    strategy_spec = {
        "class": type(strategy).__name__,
        "name": strategy.name,
        "mode": strategy.mode,
        "params": dict(strategy.params),
        "symbols": list(getattr(strategy, "symbols", ())),
        "top_n": getattr(strategy, "top_n", None),
        "buffer": getattr(strategy, "buffer", None),
        "rebalance": getattr(getattr(strategy, "rebalance", None), "value", getattr(strategy, "rebalance", None)),
        "weighting": type(getattr(strategy, "weighting", None)).__name__ if getattr(strategy, "weighting", None) is not None else None,
        "universe": getattr(strategy, "universe", None).fingerprint() if hasattr(getattr(strategy, "universe", None), "fingerprint") else None,
    }
    if benchmark is not None and not benchmark.empty:
        rows_hash = pd.util.hash_pandas_object(
            benchmark.astype("float64"), index=True
        ).to_numpy(dtype="uint64")
        benchmark_hash = hashlib.sha256(rows_hash.tobytes()).hexdigest()[:16]
    else:
        benchmark_hash = "none"
    security_status_hash = "none"
    security_status_approximation = True
    if security_status is not None:
        security_status_approximation = bool(
            getattr(security_status, "approximation", True)
        )
        status_payload = {
            "current_st": dict(getattr(security_status, "current_st", {})),
            "list_dates": dict(getattr(security_status, "list_dates", {})),
            "delist_dates": dict(getattr(security_status, "delist_dates", {})),
            "approximation": security_status_approximation,
        }
        st_frame = getattr(security_status, "st_by_date", None)
        if st_frame is not None and not st_frame.empty:
            status_payload["st_by_date"] = hashlib.sha256(
                pd.util.hash_pandas_object(st_frame, index=True)
                .to_numpy(dtype="uint64")
                .tobytes()
            ).hexdigest()[:16]
        security_status_hash = hashlib.sha256(
            canonical(status_payload).encode("utf-8")
        ).hexdigest()[:16]
    payload = {
        "strategy_source_hash": loaded.source_hash,
        "strategy_spec": strategy_spec,
        "factors": [item.to_payload() if hasattr(item, "to_payload") else item for item in factors],
        "universe": universe.fingerprint,
        "dates": [str(spec.start), str(spec.end), spec.freq],
        "data": snapshot.to_payload(),
        "rules": {
            "lot_size": spec.rules.lot_size,
            "t_plus_1": spec.rules.t_plus_1,
            "limit_up_down": spec.rules.limit_up_down,
            "suspension": spec.rules.suspension,
            "new_listing_no_limit_days": spec.rules.new_listing_no_limit_days,
            "commission_rate": spec.rules.commission_rate,
            "min_commission": spec.rules.min_commission,
            "transfer_fee_rate": spec.rules.transfer_fee_rate,
            "stamp_tax_schedule": [list(item) for item in spec.rules.stamp_tax_schedule],
        },
        "fill": spec.fill,
        "factor_source": spec.factor_source,
        "corporate_actions": spec.corporate_actions,
        "metrics_source": spec.metrics_source,
        "mode": spec.mode,
        "top_n": spec.top_n,
        "buffer": spec.buffer,
        "every_n_bars": spec.every_n_bars,
        "rebalance": spec.rebalance,
        "weighting": spec.weighting,
        "universe_kind": spec.universe,
        "initial_cash": spec.initial_cash,
        "benchmark": {
            "code": spec.benchmark,
            "hash": benchmark_hash,
            "risk_free_rate": spec.risk_free_rate,
        },
        "security_status": {
            "hash": security_status_hash,
            "approximation": security_status_approximation,
        },
        "max_gross_exposure": spec.max_gross_exposure,
        "allow_short": spec.allow_short,
        "risk_free_rate": spec.risk_free_rate,
        "engine": {"name": engine_name, "version": engine_version},
        "libs": {"numpy": np.__version__, "pandas": pd.__version__},
        "seed": int(seed),
        "code_version": APP_VERSION,
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]
