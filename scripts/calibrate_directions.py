#!/usr/bin/env python
"""因子方向校准：用实测 IC 符号校正 registry 里的设计方向。

逻辑
----
1. 从 ``factor_scorecard`` 取每个因子的**最近一次**评级记录；
2. 比较 ``direction``（设计方向）与 ``calibrated_direction``（IC>0 记 +1）；
3. 输出不一致清单；加 ``--apply`` 时回写 ``factor_registry.direction``。

注意
----
方向反转是**数据结论**，不是经济结论。若某因子的经济逻辑明确（如低波动溢价），
即便 IC 为负也应先怀疑样本/口径，而不是直接翻转——脚本默认只报告，不落库。

用法::

    python scripts/calibrate_directions.py
    python scripts/calibrate_directions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"


def _query(sql: str) -> list[dict]:
    payload = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=120).json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    names = [c["name"] for c in payload["columns"]]
    return [dict(zip(names, row)) for row in payload["dataset"]]


def _exec(sql: str) -> None:
    payload = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=120).json()
    if "error" in payload:
        raise RuntimeError(payload["error"])


LATEST = """
select factor_id, category, grade, score, ic_mean, direction, calibrated_direction, ts
from factor_scorecard
latest on ts partition by factor_id
order by score desc
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="回写 registry 的 direction")
    parser.add_argument("--min-abs-ic", type=float, default=0.005,
                        help="|IC| 低于该阈值不参与校准（噪声）")
    args = parser.parse_args()

    rows = _query(LATEST)
    if not rows:
        print("factor_scorecard 为空，先跑 scripts/factor_scorecard.py")
        return 1

    mismatch = []
    for row in rows:
        ic = float(row.get("ic_mean") or 0.0)
        if abs(ic) < args.min_abs_ic:
            continue
        design = int(row.get("direction") or 1)
        calibrated = 1 if ic > 0 else -1
        if design != calibrated:
            mismatch.append({**row, "ic_mean": ic})

    print(f"评分卡记录 {len(rows)} 个因子，方向不一致 {len(mismatch)} 个")
    if mismatch:
        print(f"{'factor_id':<38}{'IC':>8}{'设计':>6}{'实测':>6}{'评级':>6}{'分数':>7}")
        for row in mismatch:
            print(
                f"{row['factor_id']:<38}{row['ic_mean']:>+8.4f}"
                f"{int(row['direction']):>6}{int(row['calibrated_direction']):>6}"
                f"{str(row['grade']):>6}{float(row['score']):>7.1f}"
            )

    if args.apply and mismatch:
        for row in mismatch:
            text = "positive" if int(row["calibrated_direction"]) > 0 else "negative"
            _exec(
                "update factor_registry set direction = "
                f"'{text}' where factor_id = '{row['factor_id']}'"
            )
        print(f"已回写 {len(mismatch)} 条 registry.direction")
    elif mismatch:
        print("\n（只报告，未回写；确认后加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
