"""对账：因子值比较（分因子容差）。"""

from __future__ import annotations

import math
from typing import Any, Sequence

import pandas as pd

from factors.spec import Tolerance


def compare_frames(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    keys: Sequence[str] = ("ts", "code", "factor_id"),
    tolerance: Tolerance | None = None,
) -> dict[str, Any]:
    """按 key 对齐两批因子值，返回 checked/failed/最大误差。"""
    tol = tolerance or Tolerance()
    checked = 0
    failed = 0
    max_abs = 0.0
    max_rel = 0.0
    examples: list[dict[str, Any]] = []
    if left is None or right is None or left.empty or right.empty:
        return {
            "checked": 0,
            "failed": 0,
            "max_abs_diff": 0.0,
            "max_rel_diff": 0.0,
            "examples": [],
        }

    left_frame = left.copy()
    right_frame = right.copy()
    for frame in (left_frame, right_frame):
        if "ts" in frame.columns:
            frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
    merged = left_frame.merge(right_frame, on=list(keys), suffixes=("_l", "_r"))
    for _, row in merged.iterrows():
        a = row.get("value_l", row.get("value"))
        b = row.get("value_r", row.get("value"))
        if a is None or b is None:
            continue
        try:
            a = float(a)
            b = float(b)
        except (TypeError, ValueError):
            continue
        checked += 1
        abs_diff = abs(a - b)
        scale = max(abs(a), abs(b), 1.0)
        max_abs = max(max_abs, abs_diff)
        max_rel = max(max_rel, abs_diff / scale)
        if not tol.is_close(a, b):
            failed += 1
            if len(examples) < 10:
                examples.append(
                    {"keys": {key: row.get(key) for key in keys}, "left": a, "right": b}
                )

    return {
        "checked": checked,
        "failed": failed,
        "max_abs_diff": max_abs,
        "max_rel_diff": max_rel,
        "examples": examples,
    }


def record_audit(store, audit_id: str, result: dict[str, Any], **payload) -> int:
    return store.upsert_audit(
        {
            "audit_id": audit_id,
            "dataset": payload.get("dataset"),
            "factor_ids": payload.get("factor_ids"),
            "window_start": payload.get("window_start"),
            "window_end": payload.get("window_end"),
            "status": "OK" if result["failed"] == 0 else "FAILED",
            "checked": result["checked"],
            "failed": result["failed"],
            "max_abs_diff": result["max_abs_diff"],
            "max_rel_diff": result["max_rel_diff"],
            "details": str(result.get("examples")),
        }
    )
