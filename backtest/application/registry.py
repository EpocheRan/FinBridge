"""数据源 / 输出 / 引擎注册表。"""

from __future__ import annotations

from typing import Callable

from backtest.config import BacktestSettings
from backtest.domain.errors import ConfigError

_DATA_SOURCES: dict[str, Callable[..., object]] = {}
_OUTPUTS: dict[str, Callable[..., object]] = {}
_ENGINES: dict[str, Callable[..., object]] = {}


def register_data_source(name: str, factory: Callable[..., object]) -> None:
    _DATA_SOURCES[name] = factory


def register_output(name: str, factory: Callable[..., object]) -> None:
    _OUTPUTS[name] = factory


def register_engine(name: str, factory: Callable[..., object]) -> None:
    _ENGINES[name] = factory


def _questdb_source():
    from backtest.infrastructure.data.questdb import QuestDBDataSource

    return QuestDBDataSource()


def _console_output():
    from backtest.infrastructure.output.console import ConsoleOutput

    return ConsoleOutput()


def _csv_output(settings: BacktestSettings):
    from backtest.infrastructure.output.csv import CsvOutput

    return CsvOutput(settings.output_dir)


def _pybroker_engine():
    try:
        from backtest.infrastructure.engine.pybroker_engine import PyBrokerEngine
    except ImportError as exc:
        raise ConfigError(
            "PyBroker 未安装；请先执行 `uv sync --extra backtest`"
        ) from exc
    return PyBrokerEngine()


register_data_source("questdb", _questdb_source)
register_output("console", _console_output)
register_output("csv", _csv_output)
register_engine("pybroker", _pybroker_engine)


def create_data_source(name: str):
    factory = _DATA_SOURCES.get(str(name))
    if factory is None:
        raise ConfigError(f"未知数据源: {name!r}，可选: {sorted(_DATA_SOURCES)}")
    return factory()


def create_outputs(names: tuple[str, ...], settings: BacktestSettings):
    result = []
    for name in names:
        factory = _OUTPUTS.get(str(name))
        if factory is None:
            raise ConfigError(f"未知输出: {name!r}，可选: {sorted(_OUTPUTS)}")
        if name == "csv":
            result.append(factory(settings))
        else:
            result.append(factory())
    return result


def create_engine(name: str):
    factory = _ENGINES.get(str(name))
    if factory is None:
        raise ConfigError(f"未知引擎: {name!r}，可选: {sorted(_ENGINES)}")
    return factory()
