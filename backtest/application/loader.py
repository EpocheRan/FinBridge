"""策略文件加载与校验。"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from backtest.domain.errors import StrategyLoadError
from strategies.base import BaseStrategy

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LoadedStrategy:
    strategy: BaseStrategy
    path: Path
    source_hash: str


@dataclass(frozen=True)
class StrategyInfo:
    name: str
    mode: str
    path: Path
    params: Mapping[str, Any]
    factors: tuple[str, ...]


def _ensure_project_root() -> None:
    root = str(_PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _accepts_params(candidate) -> bool:
    """判断策略类/工厂是否能接收 ``params=``。

    不能用 ``try/except TypeError`` 兜底：``__init__`` 内部抛出的 TypeError
    会被误当成“签名不兼容”，导致参数被静默丢弃。
    """
    try:
        signature = inspect.signature(candidate)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if "params" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def load_strategy(path: str | Path, params: Mapping[str, Any] | None = None) -> LoadedStrategy:
    _ensure_project_root()
    source_path = Path(path)
    if not source_path.is_absolute():
        source_path = (_PROJECT_ROOT / source_path).resolve()
    if not source_path.exists():
        raise StrategyLoadError(f"策略文件不存在: {source_path}")

    source = source_path.read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    module_name = f"_backtest_strategy_{source_hash[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise StrategyLoadError(f"无法加载策略文件: {source_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise StrategyLoadError(f"策略文件执行失败 {source_path.name}: {exc}") from exc

    candidate = getattr(module, "STRATEGY", None)
    if candidate is None:
        raise StrategyLoadError(
            f"{source_path.name} 缺少模块级 STRATEGY（必须是类或 create_strategy 工厂）"
        )
    if not (inspect.isclass(candidate) or callable(candidate)):
        raise StrategyLoadError(
            f"{source_path.name}: STRATEGY 必须是类/工厂，不能是实例"
        )

    try:
        if _accepts_params(candidate):
            strategy = candidate(params=params)
        else:
            strategy = candidate()
            if params:
                strategy.params = {**getattr(strategy, "params", {}), **dict(params)}
                strategy.validate()
    except StrategyLoadError:
        raise
    except Exception as exc:
        raise StrategyLoadError(
            f"{source_path.name}: 策略实例化失败: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(strategy, BaseStrategy):
        raise StrategyLoadError(
            f"{source_path.name}: STRATEGY 必须继承 BaseStrategy，实际是 {type(strategy).__name__}"
        )
    return LoadedStrategy(strategy=strategy, path=source_path, source_hash=source_hash)


def list_strategies(models_dir: str | Path = "strategies/models") -> list[StrategyInfo]:
    base = Path(models_dir)
    if not base.is_absolute():
        base = (_PROJECT_ROOT / base).resolve()
    result: list[StrategyInfo] = []
    if not base.exists():
        return result
    for path in sorted(base.glob("*.py")):
        if path.name.startswith("_"):
            continue
        loaded = load_strategy(path)
        strategy = loaded.strategy
        result.append(
            StrategyInfo(
                name=strategy.name,
                mode=strategy.mode,
                path=path,
                params=dict(strategy.params),
                factors=tuple(item.name for item in strategy.factors()),
            )
        )
    return result
