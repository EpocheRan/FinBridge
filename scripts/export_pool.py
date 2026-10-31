#!/usr/bin/env python
"""把研究用的样本池导出成回测可用的显式股票池模块。

背景：`backtest` 层目前只支持 explicit universe（`runner._resolve_universe`
对 all_a/index 直接报错），而研究层（`research.report.panel.load_panel`）
已经有成熟的选池逻辑。本脚本负责把两边接上：

    python scripts/export_pool.py --pool-date 2017-11-01 --size 200 \
        --out strategies/pools/pool_2017_200.py

导出的模块只有一个常量 ``POOL_2017_200``，策略文件直接 import 即可::

    from strategies.pools.pool_2017_200 import POOL_2017_200
    universe = Universe.explicit(POOL_2017_200)

注意：固定入池日 = 固定成分 = 幸存者偏差，回测结论需按此折价；
要消除它，得先把走 `index_weight` 的 PIT 成分池接进回测层。
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
except Exception:  # noqa: BLE001
    pass

from research.report.panel import load_panel  # noqa: E402

_TEMPLATE_HEAD = '''"""{date} 成交额前 {size} 只（沪深主板/创业板/科创板，剔除北交所）。

由 ``scripts/export_pool.py`` 从 ``research.report.panel.load_panel`` 的选池逻辑导出，
供回测显式股票池使用。回测层目前只支持 explicit universe，池子固定意味着幸存者偏差，
结论需按此折价。
"""

from __future__ import annotations

{name}: tuple[str, ...] = (
'''

_TEMPLATE_TAIL = ''')

__all__ = ["{name}"]
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="导出回测用显式股票池")
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--start", default="2019-01-01", help="只用于校验取数，不影响选池")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    panel = load_panel(args.pool_date, args.size, args.start)
    codes = sorted(panel.symbols)
    if not codes:
        print("样本池为空，检查 pool_date 是否有行情")
        return 1

    var_name = "POOL_" + args.pool_date.replace("-", "_") + f"_{len(codes)}"
    body = [_TEMPLATE_HEAD.format(date=args.pool_date, size=len(codes), name=var_name)]
    for i in range(0, len(codes), 10):
        body.append("    " + ", ".join(repr(c) for c in codes[i : i + 10]) + ",\n")
    body.append(_TEMPLATE_TAIL.format(name=var_name))

    target = Path(args.out)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(body), encoding="utf-8", newline="\n")

    print(f"{len(codes)} 只 → {target}")
    print(f"策略里用: from {target.relative_to(PROJECT_ROOT).with_suffix('').as_posix().replace('/', '.')} import {var_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
