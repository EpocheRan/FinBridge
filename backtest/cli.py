"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from backtest.application.loader import list_strategies, load_strategy
from backtest.application.registry import create_data_source
from backtest.bootstrap import build_runner, load_settings
from backtest.domain.errors import BacktestError
from backtest.research import run_research


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
    if args.benchmark is not None:
        overrides["benchmark"] = args.benchmark or None
    if args.corporate_actions:
        overrides["corporate_actions"] = args.corporate_actions
    if args.risk_free_rate is not None:
        overrides["risk_free_rate"] = float(args.risk_free_rate)
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


def _parse_grid(items: list[str] | None) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--grid 需要 key=v1,v2，收到: {item!r}")
        key, values = item.split("=", 1)
        parsed = [_parse_value(part) for part in values.split(",") if part.strip()]
        if not parsed:
            raise SystemExit(f"--grid 参数 {key!r} 没有取值")
        result[key.strip()] = parsed
    return result


def _cmd_optimize(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.data:
        settings = replace(settings, data=args.data)
    runner = build_runner(settings=settings)
    grid = _parse_grid(args.grid)
    common = dict(
        grid=grid,
        params=_parse_params(args.param),
        metric=args.metric,
        maximize=not args.minimize,
        freq=args.freq,
        initial_cash=args.initial_cash,
        seed=args.seed,
        max_trials=args.max_trials,
    )
    if args.windows and args.windows > 0:
        result = runner.walkforward(
            args.strategy,
            start=args.start,
            end=args.end,
            windows=args.windows,
            train_size=args.train_size,
            lookahead=args.lookahead,
            **common,
        )
        print("walk-forward folds:")
        print(result.folds_frame().to_string(index=False))
    else:
        result = runner.optimize(
            args.strategy, start=args.start, end=args.end, **common
        )
        print("trials:")
        print(result.trials_frame().drop(columns=["params"], errors="ignore").to_string(index=False))
    print(f"metric: {result.metric}")
    print(f"best_params: {dict(result.best_params or {})}")
    print(f"best_score: {result.best_score}")
    return 0


def _cmd_research(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.data:
        settings = replace(settings, data=args.data)
    codes = tuple(
        part.strip() for part in str(args.codes).split(",") if part.strip()
    )
    if not codes:
        raise SystemExit("--codes 不能为空")
    horizons = tuple(
        int(part.strip()) for part in str(args.horizons).split(",") if part.strip()
    )
    report = run_research(
        data_source=create_data_source(settings.data),
        codes=codes,
        factor=args.factor,
        start=args.start,
        end=args.end,
        freq=args.freq,
        horizons=horizons,
        q=args.q,
        top_n=args.top_n,
    )
    print(f"factor: {report.factor}")
    print("IC summary:")
    print(report.summary.round(6).to_string())
    print("turnover:")
    if report.turnover.empty:
        print("  (no observations)")
    else:
        print(
            f"  mean={report.turnover.mean():.6f} last={report.turnover.iloc[-1]:.6f}"
        )
    outputs = {
        part.strip() for part in str(args.out or "console").split(",") if part.strip()
    }
    if "csv" in outputs:
        target = report.write_csv(
            Path(settings.output_dir) / "research" / report.factor
        )
        print(f"research csv: {target}")
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
    run.add_argument("--benchmark", default=None, help="benchmark 代码；空字符串表示关闭")
    run.add_argument(
        "--corporate-actions",
        default=None,
        choices=["off", "approx"],
        help="公司行为近似开关",
    )
    run.add_argument("--risk-free-rate", type=float, default=None)
    run.add_argument("--param", action="append", default=None, help="覆盖参数 key=value")
    run.set_defaults(func=_cmd_run)

    list_cmd = sub.add_parser("list", help="列出策略")
    list_cmd.set_defaults(func=_cmd_list)

    optimize = sub.add_parser("optimize", help="参数网格优化 / walk-forward")
    optimize.add_argument("strategy")
    optimize.add_argument("--start", required=True)
    optimize.add_argument("--end", required=True)
    optimize.add_argument("--grid", action="append", default=None, help="如 k=1,2,3")
    optimize.add_argument("--param", action="append", default=None, help="基准参数 key=value")
    optimize.add_argument("--metric", default="sharpe")
    optimize.add_argument("--minimize", action="store_true")
    optimize.add_argument("--windows", type=int, default=0, help=">0 时走 walk-forward")
    optimize.add_argument("--train-size", type=float, default=0.6)
    optimize.add_argument("--lookahead", type=int, default=1)
    optimize.add_argument("--max-trials", type=int, default=100)
    optimize.add_argument("--freq", default="d")
    optimize.add_argument("--data", default=None)
    optimize.add_argument("--initial-cash", type=float, default=None)
    optimize.add_argument("--seed", type=int, default=None)
    optimize.set_defaults(func=_cmd_optimize)

    research = sub.add_parser("research", help="因子研究（IC/分层/换手）")
    research.add_argument("--factor", required=True, help="如 mom20 / atr14")
    research.add_argument("--codes", required=True, help="逗号分隔的股票代码")
    research.add_argument("--start", required=True)
    research.add_argument("--end", required=True)
    research.add_argument("--freq", default="d")
    research.add_argument("--data", default=None, help="数据源: questdb")
    research.add_argument("--out", default="console", help="输出: console,csv")
    research.add_argument("--horizons", default="1,5,20")
    research.add_argument("--q", type=int, default=5)
    research.add_argument("--top-n", type=int, default=30)
    research.set_defaults(func=_cmd_research)

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
