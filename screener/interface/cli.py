"""Screener CLI。

在没有定时任务的环境里，用这些命令做一次性构建/修复。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from screener.bootstrap import build_screener_application
from screener.domain.models import BuildRequest, UniverseSpec

logger = logging.getLogger("Screener.CLI")


def _codes(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _run_job(args) -> int:
    app = build_screener_application()
    codes = _codes(args.codes)
    universe = UniverseSpec(
        codes=None if codes is None else codes,
        all_market=codes is None,
    )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    request = BuildRequest(
        dataset="screener_daily",
        start=start,
        end=end,
        codes=codes,
        force=args.force,
        formula_version=app.settings.daily.formula_version,
    )
    state = app.orchestrator.ensure_job(
        request,
        universe_key=universe.universe_key,
        universe_payload=universe.payload,
        force=args.force,
    )
    print(
        f"build_id={state.build_id} status={state.status.value} "
        f"quality={state.quality.value} coverage={state.coverage_ratio:.4f}"
    )
    return 0 if state.status.value in ("DONE", "PARTIAL") else 1


def _run_baseline(args) -> int:
    app = build_screener_application()
    codes = _codes(args.codes)
    if not codes:
        print("baseline 需要 --codes", file=sys.stderr)
        return 2
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    report = app.baseline_service.ensure(
        codes, as_of=as_of, freq=args.freq, force=args.force
    )
    print(
        f"requested={report.requested_codes} available={report.available_codes} "
        f"complete={report.complete}"
    )
    return 0 if report.complete else 1


def _run_benchmark(args) -> int:
    app = build_screener_application()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    report = app.service.market.ensure_benchmark(start, end)
    print(
        f"benchmark={app.settings.benchmark_code} complete={report.complete} "
        f"available={report.available_codes}/{report.requested_codes}"
    )
    return 0 if report.complete else 1


def _run_status(args) -> int:
    app = build_screener_application()
    states = app.service.status(args.build_id)
    if states is None:
        print("not found")
        return 1
    if not isinstance(states, list):
        states = [states]
    for state in states:
        print(
            f"{state.build_id} {state.dataset} {state.status.value} "
            f"{state.quality.value} {state.progress_current}/{state.progress_total} "
            f"coverage={state.coverage_ratio:.4f}"
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screener 构建/查询 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily", help="构建日频因子")
    daily.add_argument("--start", required=True)
    daily.add_argument("--end", required=True)
    daily.add_argument("--codes", default=None)
    daily.add_argument("--force", action="store_true")
    daily.set_defaults(func=_run_job)

    repair = subparsers.add_parser("repair", help="修复/重建日频因子")
    repair.add_argument("--start", required=True)
    repair.add_argument("--end", required=True)
    repair.add_argument("--codes", default=None)
    repair.add_argument("--force", action="store_true", default=True)
    repair.set_defaults(func=_run_job)

    baseline = subparsers.add_parser("baseline", help="构建分时基线")
    baseline.add_argument("--as-of", default=None)
    baseline.add_argument("--codes", required=True)
    baseline.add_argument("--freq", default="5m")
    baseline.add_argument("--force", action="store_true")
    baseline.set_defaults(func=_run_baseline)

    benchmark = subparsers.add_parser("benchmark", help="补齐基准指数日线")
    benchmark.add_argument("--start", required=True)
    benchmark.add_argument("--end", required=True)
    benchmark.set_defaults(func=_run_benchmark)

    status = subparsers.add_parser("status", help="查看构建状态")
    status.add_argument("--build-id", default=None)
    status.set_defaults(func=_run_status)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
