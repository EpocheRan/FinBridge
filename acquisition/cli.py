"""acquisition CLI：统一补齐 / 单代码日期段 / 状态。"""

from __future__ import annotations

import argparse
import json
from datetime import date

from .orchestrator import DATASET_ORDER, Scope, build_orchestrator


def _datasets(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _financial_reset_warning(start_year: int) -> str | None:
    """检查 financial 游标：若本次 start_year 会触发重置，返回提示。"""
    try:
        from data_interface.api.runtime import get_sync_service

        state = get_sync_service().status().get("financial") or {}
    except Exception:
        return None
    payload = state.get("payload")
    try:
        payload = json.loads(payload) if payload else {}
    except Exception:
        payload = {}
    stored = payload.get("start_year")
    if stored is not None and int(stored) != int(start_year):
        return (
            f"financial 库内游标 start_year={stored}，本次 --start 会把它重置为 "
            f"{start_year} 并从头按批重跑；已自动跳过 financial"
            "（需要强制请加 --force-financial）"
        )
    return None


def _resolve_datasets(args) -> tuple[str, ...]:
    """确定本次要跑的数据集；对 financial 做重置检查/显式排除。"""
    selected = _datasets(args.datasets) or DATASET_ORDER
    if getattr(args, "no_financial", False):
        print("[acquisition] 已按 --no-financial 排除 financial")
        return tuple(item for item in selected if item != "financial")
    if "financial" in selected and not getattr(args, "force_financial", False):
        warning = _financial_reset_warning(date.fromisoformat(args.start).year)
        if warning:
            print(f"[acquisition] WARN: {warning}")
            selected = tuple(item for item in selected if item != "financial")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="acquisition 整合同步")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="全市场补齐到最近完整交易日")
    run.add_argument("--start", default="2020-01-01")
    run.add_argument("--end", default=None)
    run.add_argument("--datasets", default=None, help="逗号分隔，默认全部")
    run.add_argument("--factor", default=None, help="逗号分隔 factor_id，仅预计算这些因子")
    run.add_argument("--loop", action="store_true", help="分批数据集自动续跑到完成/超时")
    run.add_argument("--max-rounds", type=int, default=50, help="每个数据集最多自动续跑轮数")
    run.add_argument("--timeout", type=float, default=0.0, help="每个数据集总超时秒数，0=不限")
    run.add_argument("--no-financial", action="store_true", help="显式排除 financial（日常补数据推荐）")
    run.add_argument("--force-financial", action="store_true", help="即使会重置也运行 financial")

    update = sub.add_parser("update", help="单代码/板块 + 日期段")
    update.add_argument("--code", default=None)
    update.add_argument("--board", default=None)
    update.add_argument("--start", required=True)
    update.add_argument("--end", required=True)
    update.add_argument("--datasets", default=None)
    update.add_argument("--factor", default=None, help="逗号分隔 factor_id，仅预计算这些因子")
    update.add_argument("--loop", action="store_true", help="分批数据集自动续跑到完成/超时")
    update.add_argument("--max-rounds", type=int, default=50)
    update.add_argument("--timeout", type=float, default=0.0)
    update.add_argument("--no-financial", action="store_true", help="显式排除 financial")
    update.add_argument("--force-financial", action="store_true", help="即使会重置也运行 financial")

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
    datasets = _resolve_datasets(args)
    if not datasets:
        print("[acquisition] 没有需要执行的数据集（financial 被跳过）")
        return 0
    scope = Scope(
        start=start,
        end=end,
        codes=codes,
        boards=boards,
        datasets=datasets,
        factor_ids=_datasets(getattr(args, "factor", None)),
    )
    orchestrator = build_orchestrator(
        auto_continue=args.loop,
        max_rounds=args.max_rounds,
        timeout_seconds=args.timeout,
    )
    result = orchestrator.run(scope)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if not result["errors"] else 1
