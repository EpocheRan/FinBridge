"""因子注册表：canonical id、别名、DAG、版本哈希。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from typing import Any, Iterable

from .spec import FactorSpec, Materialization
from .taxonomy import Category, normalize_category

logger = logging.getLogger("Factors.Registry")

#: canonical factor_id -> spec
FACTOR_REGISTRY: dict[str, FactorSpec] = {}
#: alias -> (factor_id, window_override)
ALIASES: dict[str, tuple[str, int | None]] = {}


def register_factor(spec: FactorSpec, *, replace: bool = False) -> FactorSpec:
    """注册一个因子；canonical 与 alias 冲突都直接失败。"""
    if not isinstance(spec, FactorSpec):
        raise TypeError("register_factor 需要 FactorSpec")
    if spec.factor_id in ALIASES:
        raise ValueError(
            f"factor_id {spec.factor_id!r} 已被 alias {ALIASES[spec.factor_id]!r} 占用"
        )
    if spec.factor_id in FACTOR_REGISTRY and not replace:
        raise ValueError(f"因子已注册: {spec.factor_id!r}")
    if replace:
        for alias, (target, _) in list(ALIASES.items()):
            if target == spec.factor_id:
                ALIASES.pop(alias)

    for alias in spec.aliases:
        if alias == spec.factor_id:
            raise ValueError(f"alias 不能与 canonical 相同: {alias!r}")
        if alias in FACTOR_REGISTRY:
            raise ValueError(f"alias {alias!r} 与已注册 canonical 冲突")
        existing = ALIASES.get(alias)
        if existing and existing[0] != spec.factor_id:
            raise ValueError(
                f"alias {alias!r} 已指向 {existing[0]!r}，不能再注册到 {spec.factor_id!r}"
            )
        ALIASES[alias] = (spec.factor_id, None)

    FACTOR_REGISTRY[spec.factor_id] = spec
    logger.debug("注册因子 %s (%s/%s)", spec.factor_id, spec.category.value, spec.subcategory)
    return spec


def unregister_factor(factor_id: str) -> None:
    FACTOR_REGISTRY.pop(factor_id, None)
    for alias, (target, _) in list(ALIASES.items()):
        if target == factor_id:
            ALIASES.pop(alias)


def reset_registry() -> None:
    FACTOR_REGISTRY.clear()
    ALIASES.clear()


def get(factor_id: str) -> FactorSpec:
    try:
        return FACTOR_REGISTRY[factor_id]
    except KeyError as exc:
        raise KeyError(f"未注册因子: {factor_id!r}") from exc


def resolve_id(name: str, window: int | None = None) -> tuple[str, int]:
    """把 canonical/alias 解析成 ``(factor_id, window)``。"""
    text = str(name).strip()
    spec = FACTOR_REGISTRY.get(text)
    if spec is not None:
        return spec.factor_id, int(window if window is not None else spec.window)
    alias = ALIASES.get(text)
    if alias is not None:
        factor_id, alias_window = alias
        chosen = window if window is not None else alias_window
        if chosen is None:
            chosen = FACTOR_REGISTRY[factor_id].window
        return factor_id, int(chosen)
    raise KeyError(f"未知因子或别名: {name!r}")


def list_factors(
    *,
    category: Category | str | None = None,
    subcategory: str | None = None,
    source_dataset: str | None = None,
    freq: str | None = None,
    window: int | None = None,
    value_kind: str | None = None,
    enabled: bool | None = None,
) -> list[FactorSpec]:
    cat = normalize_category(category) if category is not None else None
    result: list[FactorSpec] = []
    for spec in FACTOR_REGISTRY.values():
        if cat is not None and spec.category != cat:
            continue
        if subcategory is not None and spec.subcategory != subcategory:
            continue
        if source_dataset is not None and spec.source.dataset != source_dataset:
            continue
        if freq is not None and spec.freq != freq:
            continue
        if window is not None and spec.window != int(window):
            continue
        if value_kind is not None and spec.value_kind != value_kind:
            continue
        if enabled is not None and spec.enabled != enabled:
            continue
        result.append(spec)
    return sorted(result, key=lambda item: item.factor_id)


def dependency_graph() -> dict[str, tuple[str, ...]]:
    """只包含已注册 factor 依赖的 DAG。"""
    graph: dict[str, tuple[str, ...]] = {}
    for factor_id, spec in FACTOR_REGISTRY.items():
        deps = tuple(
            dep.name
            for dep in spec.dependencies
            if dep.kind == "factor" and dep.name in FACTOR_REGISTRY
        )
        graph[factor_id] = deps
    return graph


def missing_dependencies() -> list[str]:
    missing: list[str] = []
    for factor_id, spec in FACTOR_REGISTRY.items():
        for dep in spec.dependencies:
            if dep.kind == "factor" and dep.name not in FACTOR_REGISTRY:
                missing.append(f"{factor_id} -> {dep.name}")
    return sorted(missing)


def topological_order() -> list[str]:
    """Kahn 拓扑排序；有环直接失败。"""
    graph = dependency_graph()
    indegree = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            indegree[node] += 1
    # 边方向：dependency -> node
    children: dict[str, list[str]] = {node: [] for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            children.setdefault(dep, []).append(node)

    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(children.get(node, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(order) != len(graph):
        remaining = sorted(set(graph) - set(order))
        raise ValueError(f"因子依赖存在环: {remaining}")
    return order


def validate_registry(*, strict: bool = False) -> list[str]:
    """校验缺失依赖与环；strict=True 时直接抛异常。"""
    issues = [f"missing dependency: {item}" for item in missing_dependencies()]
    try:
        topological_order()
    except ValueError as exc:
        issues.append(str(exc))
    if strict and issues:
        raise ValueError("; ".join(issues))
    return issues


def registry_hash() -> str:
    """稳定哈希，供 run_id 使用。"""
    payload = [
        spec.to_payload()
        for spec in sorted(FACTOR_REGISTRY.values(), key=lambda item: item.factor_id)
    ]
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def aliases_of(factor_id: str) -> list[str]:
    return sorted(alias for alias, (target, _) in ALIASES.items() if target == factor_id)


def registered_factor_ids() -> list[str]:
    return sorted(FACTOR_REGISTRY)


def default_precompute_ids() -> list[str]:
    """默认预计算集合：STORED 必算 + AUTO 默认也算。"""
    return sorted(
        factor_id
        for factor_id, spec in FACTOR_REGISTRY.items()
        if spec.materialization in (Materialization.STORED, Materialization.AUTO)
    )


def register_many(specs: Iterable[FactorSpec], *, replace: bool = False) -> None:
    for spec in specs:
        register_factor(spec, replace=replace)
