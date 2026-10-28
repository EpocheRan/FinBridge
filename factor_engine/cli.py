"""factor_engine CLI：list / build / ensure。"""

from __future__ import annotations

import argparse
import json
import sys

from .service import FactorEngineService


def _codes(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="factor_engine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出已注册内置因子")

    build = sub.add_parser("build", help="计算并写库")
    build.add_argument("--start", required=True)
    build.add_argument("--end", required=True)
    build.add_argument("--factor", default=None, help="逗号分隔 factor_id，默认全部内置")
    build.add_argument("--codes", default=None, help="逗号分隔代码，默认全市场")

    ensure = sub.add_parser("ensure", help="确保某日因子已就绪（缺则算）")
    ensure.add_argument("--as-of", required=True)
    ensure.add_argument("--codes", required=True)
    ensure.add_argument("--factor", default=None)

    args = parser.parse_args(argv)
    service = FactorEngineService()
    if args.command == "list":
        for spec in service.list_factors():
            print(
                f"{spec.factor_id}\t{spec.category.value}/{spec.subcategory}\t"
                f"window={spec.window}\tfreq={spec.freq}\tversion={spec.version}"
            )
        return 0
    if args.command == "build":
        result = service.build(
            [item.strip() for item in (args.factor or "").split(",") if item.strip()] or None,
            args.start,
            args.end,
            _codes(args.codes),
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    if args.command == "ensure":
        result = service.ensure(
            _codes(args.codes) or [],
            args.as_of,
            [item.strip() for item in (args.factor or "").split(",") if item.strip()] or None,
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
