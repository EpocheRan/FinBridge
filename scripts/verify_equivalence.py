#!/usr/bin/env python
"""等价性回放：新 pipeline 与旧评估结果逐项比对（相对容差默认 1e-9）。

用法::

    python scripts/verify_equivalence.py                 # 自动找最近一份旧 eval CSV
    python scripts/verify_equivalence.py --baseline output/factor_reports/eval/eval_20261031T1934.csv
    python scripts/verify_equivalence.py --tol 1e-9 --universe index:000905 --start 2020-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from research.pipeline import run  # noqa: E402

FIELDS = ("ic", "spread_gross", "spread_net", "turnover")


def _find_baseline(directory: Path) -> Path:
    candidates = []
    for path in directory.glob("eval_*.csv"):
        frame = pd.read_csv(path, nrows=1)
        if "direction" in frame.columns:  # 旧 schema 有 direction，新 schema 有 profile
            candidates.append(path)
    if not candidates:
        raise SystemExit(f"{directory} 下没有旧 schema 的 eval CSV")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _rel(a: float, b: float) -> float:
    if not (np.isfinite(a) and np.isfinite(b)):
        return float("nan") if not (np.isnan(a) and np.isnan(b)) else 0.0
    return abs(a - b) / max(abs(a), abs(b), 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="等价性回放")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--dir", default="output/factor_reports/eval")
    parser.add_argument("--profile", default="quick")
    parser.add_argument("--tol", type=float, default=1e-9)
    parser.add_argument("--universe", default="index:000905")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    baseline_path = Path(args.baseline) if args.baseline else _find_baseline(Path(args.dir))
    old = pd.read_csv(baseline_path)
    print(f"基线: {baseline_path}（{len(old)} 个因子）")

    table, _ = run(
        list(old["factor_id"]),
        profile=args.profile,
        universe=args.universe,
        start=args.start,
        end=args.end,
    )
    new = table.set_index("factor_id")

    rows = []
    failed = 0
    for factor_id in old["factor_id"]:
        for field in FIELDS:
            if field not in new.columns:
                continue
            a = float(old.loc[old["factor_id"] == factor_id, field].iloc[0])
            b = float(new.loc[factor_id, field])
            diff = _rel(a, b)
            ok = (diff <= args.tol) or (np.isnan(a) and np.isnan(b))
            failed += 0 if ok else 1
            rows.append({"factor_id": factor_id, "field": field,
                         "old": a, "new": b, "rel": diff, "ok": ok})

    report = pd.DataFrame(rows)
    print(report.to_string(index=False, float_format=lambda x: f"{x:.3e}"))
    print(f"\n比对 {len(report)} 项，失败 {failed} 项，容差 {args.tol:g}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
