#!/usr/bin/env python
"""统一因子评估 CLI：一个池子、一个评估期、一套输出、一套评估项。

评估项由 profile 决定（``--profile``）；池子/区间/成本是运行参数。

用法::

    python scripts/factor_eval.py                          # default profile, 中证500
    python scripts/factor_eval.py --profile quick           # 快筛套餐
    python scripts/factor_eval.py --factors a,b --universe pool:2017-11-01:200
    python scripts/factor_eval.py --profile default --start 2019-01-01 --cost-bps 20
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from research.pipeline import CORE, run  # noqa: E402

DEFAULT_FACTORS = (
    "liquidity.turnover_std_bias_42_252",
    "sentiment.margin_balance_chg_20d",
    "sentiment.margin_short_ratio",
    "sentiment.flow_large_net_ratio_20d",
    "size.float_mv",
    "risk.vol_21d",
    "reversal.ret_21d",
    "momentum.ret_63d",
)


def _fmt(value, kind: str = "num") -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return ""
    if kind == "pct":
        return f"{number:+.2%}"
    if kind == "int":
        return f"{int(number)}"
    return f"{number:+.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="统一因子评估")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    parser.add_argument("--universe", default="all_a")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--quantile", type=float, default=0.2)
    parser.add_argument("--min-names", type=int, default=50)
    parser.add_argument("--out", default="output/factor_reports/eval")
    args = parser.parse_args()

    ids = [item.strip() for item in args.factors.split(",") if item.strip()]
    started = time.time()
    table, meta = run(
        ids,
        profile=args.profile,
        universe=args.universe,
        start=args.start,
        end=args.end,
        horizon=args.horizon,
        cost_bps=args.cost_bps,
        quantile=args.quantile,
        min_names=args.min_names,
    )
    profile = meta["profile"]
    panel = meta["panel"]
    print(
        f"profile {profile['name']} v{profile['version']}（{len(meta['items'])} 项）｜"
        f"{panel['label']}｜{panel['codes']} 只 / {panel['dates']} 个交易日｜"
        f"{time.time() - started:.1f}s\n"
    )

    columns = [c for c in table.columns if c not in CORE and c not in ("note",)]
    header = "| 因子 | " + " | ".join(columns) + " |"
    print(header)
    print("|" + "---|" * (len(columns) + 1))
    pct = {"spread_gross", "spread_net", "coverage", "turnover", "mono",
           "ic_positive_years", "ic_1d", "ic_5d", "ic_10d", "ic_20d"}
    ints = {"n_days", "n_indep", "n_periods", "avg_names"}
    for _, row in table.iterrows():
        cells = []
        for column in columns:
            if column in ints:
                cells.append(_fmt(row.get(column), "int"))
            elif column in pct:
                cells.append(_fmt(row.get(column), "pct"))
            else:
                cells.append(_fmt(row.get(column), "num"))
        print(f"| `{row['factor_id']}` | " + " | ".join(cells) + " |")

    notes = table.iloc[0:0]
    if "note" in table:
        notes = table[table["note"].notna() & (table["note"].astype(str) != "nan")]
    if not notes.empty:
        print("\n备注：")
        for _, row in notes.iterrows():
            print(f"  - {row['factor_id']}: {row['note']}")

    print("\n数据完整性：")
    for domain in meta["domains"]:
        print(f"  {domain['dataset']:<22} {domain['start']} ~ {domain['end']}  [{domain['status']}]")
    if meta["not_selected"]:
        print(f"未启用评估项: {', '.join(meta['not_selected'])}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"eval_{time.strftime('%Y%m%dT%H%M')}.csv"
    table.to_csv(target, index=False, encoding="utf-8-sig")
    print(f"\nCSV → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
