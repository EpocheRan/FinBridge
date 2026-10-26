"""run_id 计算：同输入同 id，同 id 幂等。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

APP_VERSION = "0.3.0"


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
        "initial_cash": spec.initial_cash,
        "benchmark": spec.benchmark,
        "max_gross_exposure": spec.max_gross_exposure,
        "allow_short": spec.allow_short,
        "engine": {"name": engine_name, "version": engine_version},
        "libs": {"numpy": np.__version__, "pandas": pd.__version__},
        "seed": int(seed),
        "code_version": APP_VERSION,
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]
