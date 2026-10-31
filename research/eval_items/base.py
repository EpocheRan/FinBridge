"""评估项骨架：目录（G）、执行器（D 的引擎）。

一个评估项 = 一个自包含文件：声明输入、输出、依赖，实现 compute()。
加一项评估 = 加一个文件并在 ``__init__`` 里 import，其余代码不动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

import pandas as pd

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ItemSpec:
    id: str
    title: str = ""
    requires: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    default_params: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ItemResult:
    id: str
    values: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_OK
    note: str = ""

    @classmethod
    def unavailable(cls, item_id: str, note: str) -> "ItemResult":
        return cls(id=item_id, status=STATUS_UNAVAILABLE, note=note)

    @classmethod
    def failed(cls, item_id: str, exc: Exception) -> "ItemResult":
        return cls(
            id=item_id,
            status=STATUS_UNAVAILABLE,
            note=f"{type(exc).__name__}: {exc}",
        )


class EvalItem(Protocol):
    spec: ItemSpec

    def compute(self, ctx: "EvalContext", params: Mapping[str, Any]) -> ItemResult: ...


@dataclass
class EvalContext:
    """D 构造的只读上下文：所有评估项共享，避免重复计算。"""

    factor_id: str
    direction: int
    values: pd.DataFrame          # date × symbol 因子值（原始方向）
    signal: pd.DataFrame          # values × direction
    forward: pd.DataFrame         # 前向收益
    membership: Any               # PIT 成分
    panel_dates: pd.DatetimeIndex # 评估期交易日
    quantile: float
    min_names: int
    cost_bps: float
    horizon: int
    extras: dict[str, Any] = field(default_factory=dict)
    results: dict[str, ItemResult] = field(default_factory=dict)

    def put(self, result: ItemResult) -> None:
        self.results[result.id] = result


_REGISTRY: dict[str, EvalItem] = {}


class _FunctionItem:
    """把「函数式评估项」包装成带 spec 的对象。"""

    def __init__(self, fn: Callable[..., ItemResult], spec: ItemSpec):
        self._fn = fn
        self.spec = spec

    def compute(self, ctx: "EvalContext", params: Mapping[str, Any]) -> ItemResult:
        return self._fn(ctx, params)


def item(spec: ItemSpec) -> Callable[[Any], Any]:
    def decorator(target: Any) -> Any:
        if isinstance(target, type):
            impl: EvalItem = target()
            impl.spec = spec  # type: ignore[attr-defined]
        else:
            impl = _FunctionItem(target, spec)
        _REGISTRY[spec.id] = impl
        return target

    return decorator


def catalog() -> dict[str, ItemSpec]:
    return {key: value.spec for key, value in _REGISTRY.items()}


def resolve(item_ids: list[str]) -> list[EvalItem]:
    """按 requires 做拓扑排序；缺项直接报错（不允许静默跳过）。"""
    missing = [item_id for item_id in item_ids if item_id not in _REGISTRY]
    if missing:
        raise KeyError(f"未知评估项: {missing}；可用: {sorted(_REGISTRY)}")
    ordered: list[EvalItem] = []
    seen: set[str] = set()

    def visit(item_id: str, stack: tuple[str, ...] = ()) -> None:
        if item_id in seen:
            return
        if item_id in stack:
            raise ValueError(f"评估项依赖成环: {' -> '.join(stack + (item_id,))}")
        for dep in _REGISTRY[item_id].spec.requires:
            visit(dep, stack + (item_id,))
        seen.add(item_id)
        ordered.append(_REGISTRY[item_id])

    for item_id in item_ids:
        visit(item_id)
    return ordered


def execute(
    item_ids: list[str],
    ctx: EvalContext,
    params: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ItemResult]:
    """逐项执行；单项失败只影响该项（unavailable + 原因）。"""
    params = params or {}
    results: list[ItemResult] = []
    for impl in resolve(list(item_ids)):
        spec = impl.spec
        if spec.requires and any(
            ctx.results.get(dep, ItemResult(dep)).status == STATUS_UNAVAILABLE
            for dep in spec.requires
        ):
            result = ItemResult.unavailable(spec.id, "依赖项不可用: " + ",".join(spec.requires))
        else:
            merged = {**dict(spec.default_params), **dict(params.get(spec.id, {}))}
            try:
                result = impl.compute(ctx, merged)
            except Exception as exc:  # noqa: BLE001 - 单项失败隔离
                result = ItemResult.failed(spec.id, exc)
        ctx.put(result)
        results.append(result)
    return results


def schema_for(item_ids: list[str]) -> tuple[str, ...]:
    """输出列 = 各 item.outputs 的并集，按注册顺序稳定排序。"""
    ordered = resolve(list(item_ids))
    columns: list[str] = []
    for impl in ordered:
        for name in impl.spec.outputs:
            if name not in columns:
                columns.append(name)
    return tuple(columns)


__all__ = [
    "EvalContext",
    "EvalItem",
    "ItemResult",
    "ItemSpec",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "catalog",
    "execute",
    "item",
    "resolve",
    "schema_for",
]
