"""回测管线编排。"""

from __future__ import annotations

from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from backtest.application.loader import LoadedStrategy, load_strategy
from backtest.application.ports import EnginePort, OutputPort
from backtest.application.run_id import compute_run_id
from backtest.config import BacktestSettings
from backtest.domain.errors import BacktestError
from backtest.domain.models import BacktestResult, RunSpec
from factors import build_panel, required_warmup
from strategies.base import BaseStrategy


def _engine_version() -> str:
    try:
        return version("lib-pybroker")
    except PackageNotFoundError:
        return "none"


class BacktestRunner:
    def __init__(
        self,
        *,
        settings: BacktestSettings,
        data_source,
        engine: EnginePort,
        outputs: Sequence[OutputPort],
    ):
        self.settings = settings
        self.data_source = data_source
        self.engine = engine
        self.outputs = list(outputs)

    def run(
        self,
        strategy_path: str | Path,
        *,
        start: str | date,
        end: str | date,
        freq: str = "d",
        params: Mapping[str, Any] | None = None,
        initial_cash: float | None = None,
        seed: int | None = None,
    ) -> BacktestResult:
        start_date = pd.Timestamp(start).date()
        end_date = pd.Timestamp(end).date()
        if start_date > end_date:
            raise BacktestError(f"start={start_date} 晚于 end={end_date}")

        loaded: LoadedStrategy = load_strategy(strategy_path, params)
        strategy: BaseStrategy = loaded.strategy
        if strategy.mode != "single":
            raise BacktestError(
                "MVP 只实现单股；横截面请在 engine 接入后再运行 "
                f"(strategy={strategy.name}, mode={strategy.mode})"
            )
        symbols = tuple(str(item) for item in getattr(strategy, "symbols", ()))
        if not symbols:
            raise BacktestError("单股策略没有 symbols")

        factors = list(strategy.factors())
        warmup = max(required_warmup(factors), self.settings.warmup_for(factors))
        data = self.data_source.load(symbols, start_date, end_date, warmup=warmup, freq=freq)
        panel = build_panel(data.frame, factors, start_date, end_date)

        spec = RunSpec(
            strategy_name=strategy.name,
            strategy_path=str(loaded.path),
            mode="single",
            symbols=symbols,
            start=start_date,
            end=end_date,
            freq=freq,
            initial_cash=float(initial_cash if initial_cash is not None else self.settings.initial_cash),
            params=dict(strategy.params),
            factor_source=self.settings.factor_source,
            fill=self.settings.fill,
            benchmark=self.settings.benchmark,
            rebalance=getattr(getattr(strategy, "rebalance", None), "value", self.settings.rebalance),
            max_gross_exposure=self.settings.max_gross_exposure,
            allow_short=self.settings.allow_short,
            corporate_actions=self.settings.corporate_actions,
            metrics_source="ledger",
            outputs=self.settings.outputs,
            data_source=self.settings.data,
            seed=int(seed if seed is not None else self.settings.seed),
            rules=self.settings.rules,
        )
        request = self._engine_request(
            data=data, panel=panel, strategy=strategy, spec=spec
        )
        result = self.engine.run(request)
        run_id = compute_run_id(
            loaded=loaded,
            spec=spec,
            factors=factors,
            universe=data.universe,
            snapshot=data.snapshot,
            engine_name=self.engine.name,
            engine_version=_engine_version(),
            seed=spec.seed,
        )
        result.run_id = run_id
        result.spec = spec
        artifacts = []
        for output in self.outputs:
            artifacts.extend(output.emit(result))
        result.meta["outputs"] = [
            {
                "kind": item.kind,
                "path": None if item.path is None else str(item.path),
                "table": item.table,
                "rows": item.rows,
            }
            for item in artifacts
        ]
        return result

    def _engine_request(self, *, data, panel, strategy, spec):
        from backtest.application.ports import EngineRequest

        return EngineRequest(
            spec=spec,
            data=data,
            panel=panel,
            strategy=strategy,
            universe=data.universe,
            calendar=data.calendar,
            rules=spec.rules,
            benchmark=None,
            seed=spec.seed,
        )
