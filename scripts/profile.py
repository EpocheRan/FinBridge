#!/usr/bin/env python
"""profile 管理：列出 / 查看 / 保存 / 灌默认。

profile 只存「评估什么」（评估项 + 参数 + 阈值 + 参照因子），不含池子/区间/成本。

用法::

    python scripts/profile.py --seed                      # 首次：灌 default / quick
    python scripts/profile.py --list
    python scripts/profile.py --show quick
    python scripts/profile.py --save my_quick --base quick --items ic,spread,mono
    python scripts/profile.py --save my_quick --base quick --items ic,spread --reference "" 
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

from research import profile as profile_mod  # noqa: E402
from research.eval_items import catalog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="评估 profile 管理")
    parser.add_argument("--seed", action="store_true", help="把内置 profile 灌进库")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--show", default=None)
    parser.add_argument("--catalog", action="store_true", help="列出所有可用评估项")
    parser.add_argument("--save", default=None, help="profile 名称")
    parser.add_argument("--base", default="default", help="以哪个 profile 为底")
    parser.add_argument("--items", default=None, help="逗号分隔，覆盖评估项")
    parser.add_argument("--reference", default=None)
    parser.add_argument("--description", default="")
    args = parser.parse_args()

    if args.catalog:
        for item_id, spec in sorted(catalog().items()):
            print(f"{item_id:<20} {spec.title}")
        return 0

    if args.seed:
        written = profile_mod.seed()
        print(f"已灌入: {written or '（已存在，无需灌入）'}")
        return 0

    if args.list:
        frame = profile_mod.list_profiles()
        print(frame.to_string(index=False))
        return 0

    if args.show:
        payload = profile_mod.load(args.show)
        print(f"name={payload['name']} version={payload['version']} source={payload['source']}")
        print(f"items={list(payload['items'])}")
        print(f"reference={payload['reference']}")
        print(f"hash={payload['profile_hash']}")
        return 0

    if args.save:
        base = profile_mod.load(args.base)
        items = tuple(
            item.strip() for item in (args.items or ",".join(base["items"])).split(",") if item.strip()
        )
        unknown = [i for i in items if i not in catalog()]
        if unknown:
            print(f"未知评估项: {unknown}；可用: {sorted(catalog())}")
            return 2
        reference = base["reference"] if args.reference is None else args.reference
        result = profile_mod.save(
            args.save,
            items=items,
            item_params=base.get("item_params") or {},
            thresholds=base.get("thresholds") or {},
            reference=reference,
            description=args.description or f"来自 {args.base}",
        )
        print(f"saved {result['name']} v{result['version']} hash={result['profile_hash']}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
