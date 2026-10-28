"""acquisition CLI：统一补齐 / 单代码日期段 / 状态。"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from .orchestrator import Scope, build_orchestrator


def _datasets(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="acquisition 整合同步")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="全市场补齐到最近完整交易日")
    run.add_argument("--start", default="2020-01-01")
    run.add_argument("--end", default=None)
    run.add_argument("--datasets", default=None, help="逗号分隔，默认全部")

    update = sub.add_parser("update", help="单代码/板块 + 日期段")
    update.add_argument("--code", default=None)
    update.add_argument("--board", default=None)
    update.add_argument("--start", required=True)
    update.add_argument("--end", required=True)
    update.add_argument("--datasets", default=None)

    sub.add_parser("status", help="查看各数据集游标")

    args = parser.parse_args(argv)
    if args.command == "status":
        from data_interface.api.runtime import get_sync_service

        print(json.dumps(get_sync_service().status(), ensure_ascii=False, default=str))
        return 0

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start)
    codes = (args.code,) if getattr(args, "code", None) else None
    boards = (args.board,) if getattr(args, "board", None) else None
    scope = Scope(
        start=start,
        end=end,
        codes=codes,
        boards=boards,
        datasets=_datasets(args.datasets),
    )
    result = build_orchestrator().run(scope)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
