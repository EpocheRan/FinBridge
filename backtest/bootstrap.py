"""Composition root。"""

from __future__ import annotations

from typing import Any, Mapping

from backtest.application.registry import create_data_source, create_engine, create_outputs
from backtest.application.runner import BacktestRunner
from backtest.config import BacktestSettings
from data_interface.common.config import get_config


def load_settings(config: Mapping[str, Any] | None = None) -> BacktestSettings:
    payload = config if config is not None else get_config()
    return BacktestSettings.from_dict(payload.get("backtest", {}))


def build_runner(
    *,
    settings: BacktestSettings | None = None,
    data_source=None,
    engine=None,
    outputs=None,
) -> BacktestRunner:
    settings = settings or load_settings()
    data_source = data_source if data_source is not None else create_data_source(settings.data)
    engine = engine if engine is not None else create_engine(settings.engine)
    outputs = list(outputs) if outputs is not None else create_outputs(settings.outputs, settings)
    return BacktestRunner(
        settings=settings,
        data_source=data_source,
        engine=engine,
        outputs=outputs,
    )
