"""网格优化与 walk-forward。

实现选择：每个参数组合都走完整的 ``BacktestRunner.run``（PyBroker + 账本 +
A 股规则），因此优化/滚动验证与正式回测使用同一条执行路径。代价是每个 trial
都会重新加载数据、重建因子面板；参数网格应保持较小。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd

from backtest.domain.errors import BacktestError


@dataclass(frozen=True)
class OptimizationTrial:
    params: Mapping[str, Any]
    run_id: str | None
    score: float
    metrics: Mapping[str, float]
    error: str | None = None


@dataclass(frozen=True)
class WalkforwardFold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    best_params: Mapping[str, Any] | None
    train_score: float
    test_metrics: Mapping[str, float]
    test_run_id: str | None
    test_error: str | None = None
    trials: tuple[OptimizationTrial, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OptimizationResult:
    metric: str
    maximize: bool
    best_params: Mapping[str, Any] | None
    best_score: float | None
    trials: tuple[OptimizationTrial, ...] = field(default_factory=tuple)
    folds: tuple[WalkforwardFold, ...] = field(default_factory=tuple)

    def trials_frame(self) -> pd.DataFrame:
        rows = []
        for trial in self.trials:
            rows.append(
                {
                    "params": dict(trial.params),
                    "run_id": trial.run_id,
                    "score": trial.score,
                    "error": trial.error,
                    **{f"metric_{key}": value for key, value in trial.metrics.items()},
                }
            )
        return pd.DataFrame(rows)

    def folds_frame(self) -> pd.DataFrame:
        rows = []
        for fold in self.folds:
            rows.append(
                {
                    "train_start": fold.train_start,
                    "train_end": fold.train_end,
                    "test_start": fold.test_start,
                    "test_end": fold.test_end,
                    "best_params": dict(fold.best_params or {}),
                    "train_score": fold.train_score,
                    "test_error": fold.test_error,
                    **{
                        f"test_{key}": value
                        for key, value in fold.test_metrics.items()
                    },
                }
            )
        return pd.DataFrame(rows)


def _grid_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    return (value,)


def _combinations(
    grid: Mapping[str, Any] | None, max_trials: int | None
) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = list(grid)
    values = [_grid_values(grid[key]) for key in keys]
    if any(len(item) == 0 for item in values):
        raise BacktestError("参数网格不能包含空序列")
    combos: list[dict[str, Any]] = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
        if max_trials is not None and len(combos) >= int(max_trials):
            break
    if not combos:
        raise BacktestError("参数网格没有产生任何组合")
    return combos


def _score_value(metrics: Mapping[str, float], metric: str, maximize: bool) -> float:
    value = metrics.get(metric)
    if value is None:
        return -math.inf if maximize else math.inf
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -math.inf if maximize else math.inf
    if not math.isfinite(number):
        return -math.inf if maximize else math.inf
    return number


def _pick_best(
    trials: Sequence[OptimizationTrial], maximize: bool
) -> OptimizationTrial | None:
    valid = [
        trial
        for trial in trials
        if trial.error is None and math.isfinite(float(trial.score))
    ]
    if not valid:
        return None
    return max(valid, key=lambda item: item.score) if maximize else min(
        valid, key=lambda item: item.score
    )


def _evaluate(
    runner,
    strategy_path,
    *,
    start: date,
    end: date,
    grid: Mapping[str, Any] | None,
    base_params: Mapping[str, Any] | None,
    metric: str,
    maximize: bool,
    freq: str,
    initial_cash: float | None,
    seed: int | None,
    max_trials: int | None,
) -> tuple[OptimizationTrial, ...]:
    from backtest.application.runner import BacktestRunner

    silent = BacktestRunner(
        settings=runner.settings,
        data_source=runner.data_source,
        engine=runner.engine,
        outputs=(),
    )
    trials: list[OptimizationTrial] = []
    for combo in _combinations(grid, max_trials):
        params = {**dict(base_params or {}), **combo}
        try:
            result = silent.run(
                strategy_path,
                start=start,
                end=end,
                freq=freq,
                params=params,
                initial_cash=initial_cash,
                seed=seed,
            )
        except Exception as exc:  # 单个 trial 失败不应终止整个网格
            trials.append(
                OptimizationTrial(
                    params=params,
                    run_id=None,
                    score=-math.inf if maximize else math.inf,
                    metrics={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        trials.append(
            OptimizationTrial(
                params=params,
                run_id=result.run_id,
                score=_score_value(result.metrics, metric, maximize),
                metrics=dict(result.metrics),
            )
        )
    return tuple(trials)


def optimize(
    runner,
    strategy_path,
    *,
    start: str | date,
    end: str | date,
    grid: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    metric: str = "sharpe",
    maximize: bool = True,
    freq: str = "d",
    initial_cash: float | None = None,
    seed: int | None = None,
    max_trials: int | None = 100,
) -> OptimizationResult:
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end).date()
    trials = _evaluate(
        runner,
        strategy_path,
        start=start_date,
        end=end_date,
        grid=grid,
        base_params=params,
        metric=metric,
        maximize=maximize,
        freq=freq,
        initial_cash=initial_cash,
        seed=seed,
        max_trials=max_trials,
    )
    best = _pick_best(trials, maximize)
    return OptimizationResult(
        metric=metric,
        maximize=maximize,
        best_params=None if best is None else dict(best.params),
        best_score=None if best is None else best.score,
        trials=trials,
    )


def walkforward(
    runner,
    strategy_path,
    *,
    start: str | date,
    end: str | date,
    grid: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    metric: str = "sharpe",
    maximize: bool = True,
    windows: int = 3,
    train_size: float = 0.6,
    lookahead: int = 1,
    freq: str = "d",
    initial_cash: float | None = None,
    seed: int | None = None,
    max_trials: int | None = 100,
) -> OptimizationResult:
    if windows <= 0:
        raise BacktestError("windows 必须 > 0")
    if not 0.0 < float(train_size) < 1.0:
        raise BacktestError("train_size 必须在 (0, 1)")
    if lookahead < 0:
        raise BacktestError("lookahead 不能为负")
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    dates = pd.bdate_range(start_ts, end_ts)
    if len(dates) < windows * 3:
        raise BacktestError("日期范围太短，无法切分 walk-forward 窗口")
    block = max(2, len(dates) // int(windows))
    train_len = max(1, int(block * float(train_size)))
    test_len = max(1, block - train_len - int(lookahead))
    folds: list[WalkforwardFold] = []
    for index in range(int(windows)):
        train_start_idx = index * block
        train_end_idx = train_start_idx + train_len - 1
        test_start_idx = train_end_idx + 1 + int(lookahead)
        test_end_idx = min(test_start_idx + test_len - 1, len(dates) - 1)
        if train_start_idx >= len(dates) or test_start_idx >= len(dates):
            break
        train_start = dates[train_start_idx].date()
        train_end = dates[train_end_idx].date()
        test_start = dates[test_start_idx].date()
        test_end = dates[test_end_idx].date()
        if train_start > train_end or test_start > test_end:
            continue
        trials = _evaluate(
            runner,
            strategy_path,
            start=train_start,
            end=train_end,
            grid=grid,
            base_params=params,
            metric=metric,
            maximize=maximize,
            freq=freq,
            initial_cash=initial_cash,
            seed=seed,
            max_trials=max_trials,
        )
        best = _pick_best(trials, maximize)
        if best is None:
            folds.append(
                WalkforwardFold(
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    best_params=None,
                    train_score=-math.inf if maximize else math.inf,
                    test_metrics={},
                    test_run_id=None,
                    trials=trials,
                )
            )
            continue
        from backtest.application.runner import BacktestRunner

        silent = BacktestRunner(
            settings=runner.settings,
            data_source=runner.data_source,
            engine=runner.engine,
            outputs=(),
        )
        test_error = None
        try:
            test_result = silent.run(
                strategy_path,
                start=test_start,
                end=test_end,
                freq=freq,
                params=dict(best.params),
                initial_cash=initial_cash,
                seed=seed,
            )
            test_metrics = dict(test_result.metrics)
            test_run_id = test_result.run_id
        except Exception as exc:
            test_metrics = {}
            test_run_id = None
            test_error = f"{type(exc).__name__}: {exc}"
        folds.append(
            WalkforwardFold(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                best_params=dict(best.params),
                train_score=best.score,
                test_metrics=test_metrics,
                test_run_id=test_run_id,
                test_error=test_error,
                trials=trials,
            )
        )
    valid_folds = [
        fold
        for fold in folds
        if fold.test_run_id is not None
        and fold.test_metrics
        and math.isfinite(_score_value(fold.test_metrics, metric, maximize))
    ]
    if valid_folds:
        # 不在 test 上挑“最好窗口”（会二次过拟合）；best_score 用各窗口
        # test metric 的均值，best_params 取最后一个窗口的选参结果供下一段使用。
        best_score = sum(
            _score_value(fold.test_metrics, metric, maximize)
            for fold in valid_folds
        ) / len(valid_folds)
        best_params = dict(valid_folds[-1].best_params or {})
    else:
        best_score = None
        best_params = None
    return OptimizationResult(
        metric=metric,
        maximize=maximize,
        best_params=best_params,
        best_score=best_score,
        trials=(),
        folds=tuple(folds),
    )
