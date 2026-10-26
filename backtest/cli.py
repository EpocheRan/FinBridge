"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from typing import Any

from backtest.application.loader import list_strategies, load_strategy
from backtest.bootstrap import build_runner, load_settings
from backtest.domain.errors import BacktestError


def _parse_value(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_params(items: list[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--param 需要 key=value，收到: {item!r}")
        key, value = item.split("=", 1)
        result[key.strip()] = _parse_value(value)
    return result


def _cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    overrides = {}
    if args.data:
        overrides["data"] = args.data
    if args.out:
        overrides["outputs"] = tuple(
            part.strip() for part in args.out.split(",") if part.strip()
        )
    if args.initial_cash is not None:
        overrides["initial_cash"] = float(args.initial_cash)
    if args.factor_source:
        overrides["factor_source"] = args.factor_source
    if overrides:
        settings = replace(settings, **overrides)
    runner = build_runner(settings=settings)
    result = runner.run(
        args.strategy,
        start=args.start,
        end=args.end,
        freq=args.freq,
        params=_parse_params(args.param),
        initial_cash=args.initial_cash,
        seed=args.seed,
    )
    if "console" not in settings.outputs:
        print(f"run_id: {result.run_id}")
        for key, value in sorted(result.metrics.items()):
            print(f"{key}: {value}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    items = list_strategies()
    if not items:
        print("没有找到策略（strategies/models/*.py）")
        return 0
    for item in items:
        print(f"{item.name}\t{item.mode}\t{item.path.name}\tfactors={','.join(item.factors)}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    loaded = load_strategy(args.strategy)
    strategy = loaded.strategy
    print(f"name: {strategy.name}")
    print(f"mode: {strategy.mode}")
    print(f"params: {dict(strategy.params)}")
    print(f"factors: {[item.name for item in strategy.factors()]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backtest")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行回测")
    run.add_argument("strategy", help="策略文件路径")
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--freq", default="d")
    run.add_argument("--data", default=None, help="数据源: questdb")
    run.add_argument("--out", default=None, help="输出: console,csv")
    run.add_argument("--factor-source", default=None, choices=["auto", "stored", "compute"])
    run.add_argument("--initial-cash", type=float, default=None)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--param", action="append", default=None, help="覆盖参数 key=value")
    run.set_defaults(func=_cmd_run)

    list_cmd = sub.add_parser("list", help="列出策略")
    list_cmd.set_defaults(func=_cmd_list)

    validate = sub.add_parser("validate", help="校验策略文件")
    validate.add_argument("strategy")
    validate.set_defaults(func=_cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BacktestError as exc:
        print(f"错误: {exc}")
        return 1
    except Exception as exc:
        print(f"未预期错误: {type(exc).__name__}: {exc}")
        return 1
