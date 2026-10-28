"""因子元数据：FactorSpec / SourceSpec / Dependency / Tolerance。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Literal

from .library import PriceBasis, Unit
from .taxonomy import Category, normalize_category, normalize_subcategory

Freq = Literal["1d", "5m", "1w", "event"]
DependencyKind = Literal["raw", "factor", "ref"]
AsOfPolicy = Literal["trade_date", "available_at", "none"]


class Materialization(str, Enum):
    """因子落库/消费方式标记。

    - STORED：必须预计算落库；回测只读，缺失即报错。
    - INLINE：不落库；永远逐 bar 现算，必须有 compute。
    - AUTO：stored 优先，缺失且有 compute 则回退 inline。
    """

    STORED = "stored"
    INLINE = "inline"
    AUTO = "auto"


_VALID_MATERIALIZATION = {item.value for item in Materialization}

_FACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_VALID_FREQ = {"1d", "5m", "1w", "event"}
_VALID_KIND = {"raw", "factor", "ref"}
_VALID_AS_OF = {"trade_date", "available_at", "none"}


@dataclass(frozen=True)
class Tolerance:
    """对账/增量抽检容差。

    默认相对 1e-9，近零用绝对下限；避免市值/成交额这类大值因绝对误差误报。
    """

    mode: Literal["relative", "absolute"] = "relative"
    value: float = 1e-9
    floor: float = 1e-12

    def __post_init__(self) -> None:
        if self.mode not in ("relative", "absolute"):
            raise ValueError(f"未知容差模式: {self.mode!r}")
        if self.value <= 0:
            raise ValueError("tolerance.value 必须 > 0")
        if self.floor < 0:
            raise ValueError("tolerance.floor 不能为负")

    def is_close(self, a: float, b: float) -> bool:
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        if math.isinf(a) or math.isinf(b):
            return a == b
        if self.mode == "absolute":
            return abs(a - b) <= self.value
        scale = max(abs(a), abs(b), 1.0)
        return abs(a - b) <= max(self.floor, self.value * scale)

    def to_payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "value": self.value, "floor": self.floor}


def close_enough(a: float, b: float, tolerance: Tolerance | None = None) -> bool:
    return (tolerance or Tolerance()).is_close(a, b)


@dataclass(frozen=True)
class SourceSpec:
    """数据来源：表 / 供应商 / 字段 / 时点策略。"""

    dataset: str
    vendor: str
    fields: tuple[str, ...] = ()
    as_of: AsOfPolicy = "trade_date"

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset", str(self.dataset).strip())
        object.__setattr__(self, "vendor", str(self.vendor).strip())
        object.__setattr__(self, "fields", tuple(str(item) for item in self.fields))
        if not self.dataset or not self.vendor:
            raise ValueError("SourceSpec.dataset / vendor 不能为空")
        if self.as_of not in _VALID_AS_OF:
            raise ValueError(f"未知 as_of 策略: {self.as_of!r}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "vendor": self.vendor,
            "fields": list(self.fields),
            "as_of": self.as_of,
        }


@dataclass(frozen=True)
class Dependency:
    """依赖：raw:close / factor:momentum.ret / ref:industry。"""

    kind: DependencyKind
    name: str

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KIND:
            raise ValueError(f"未知依赖类型: {self.kind!r}")
        name = str(self.name).strip()
        if not name:
            raise ValueError("Dependency.name 不能为空")
        object.__setattr__(self, "name", name)
        if self.kind == "factor" and not _FACTOR_ID_RE.fullmatch(name):
            raise ValueError(f"factor 依赖必须是 canonical factor_id: {name!r}")

    @property
    def token(self) -> str:
        return f"{self.kind}:{self.name}"

    def to_payload(self) -> str:
        return self.token


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    name: str
    category: Category | str
    subcategory: str
    source: SourceSpec
    window: int
    freq: Freq
    value_kind: Literal["daily", "event"] = "daily"
    update_mode: Literal["shift", "window", "recursive", "event"] = "shift"
    unit: Unit = "ratio"
    direction: Literal["positive", "negative", "neutral"] = "positive"
    price_basis: PriceBasis = "qfq"
    version: str = "v1"
    source_version_policy: str = "default"
    dependencies: tuple[Dependency, ...] = ()
    warmup: int = 0
    lag: int = 0
    recompute_anchor: date | None = None
    formula: str = ""
    aliases: tuple[str, ...] = ()
    tolerance: Tolerance = field(default_factory=Tolerance)
    enabled: bool = True
    materialization: Materialization | str = Materialization.AUTO

    def __post_init__(self) -> None:
        factor_id = str(self.factor_id).strip()
        if not _FACTOR_ID_RE.fullmatch(factor_id):
            raise ValueError(
                f"factor_id 必须形如 category.name（小写、点分隔）: {factor_id!r}"
            )
        category = normalize_category(self.category)
        subcategory = normalize_subcategory(category, self.subcategory)
        if self.freq not in _VALID_FREQ:
            raise ValueError(f"未知 freq: {self.freq!r}")
        if self.value_kind not in ("daily", "event"):
            raise ValueError(f"未知 value_kind: {self.value_kind!r}")
        if self.update_mode not in ("shift", "window", "recursive", "event"):
            raise ValueError(f"未知 update_mode: {self.update_mode!r}")
        if int(self.window) < 0 or int(self.warmup) < 0 or int(self.lag) < 0:
            raise ValueError("window / warmup / lag 不能为负")
        if self.value_kind == "event" and self.source.as_of != "available_at":
            raise ValueError("event 因子必须使用 source.as_of='available_at'")

        object.__setattr__(self, "factor_id", factor_id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "subcategory", subcategory)
        object.__setattr__(self, "window", int(self.window))
        object.__setattr__(self, "warmup", int(self.warmup))
        object.__setattr__(self, "lag", int(self.lag))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "aliases", tuple(str(item) for item in self.aliases))
        if not isinstance(self.tolerance, Tolerance):
            raise TypeError("tolerance 必须是 Tolerance")
        if isinstance(self.materialization, Materialization):
            materialization = self.materialization
        else:
            text = str(self.materialization).strip().lower()
            if text not in _VALID_MATERIALIZATION:
                raise ValueError(f"未知 materialization: {self.materialization!r}")
            materialization = Materialization(text)
        object.__setattr__(self, "materialization", materialization)

    # ------------------------------------------------------------------ 标签
    @property
    def category_value(self) -> str:
        return self.category.value

    def tags(self) -> dict[str, Any]:
        """三类必填标签：类型 / 数据来源 / 窗口频率。"""
        return {
            "category": self.category.value,
            "subcategory": self.subcategory,
            "source_dataset": self.source.dataset,
            "source_vendor": self.source.vendor,
            "source_fields": list(self.source.fields),
            "window": self.window,
            "freq": self.freq,
            "lag": self.lag,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "name": self.name,
            "category": self.category.value,
            "subcategory": self.subcategory,
            "source": self.source.to_payload(),
            "window": self.window,
            "freq": self.freq,
            "value_kind": self.value_kind,
            "update_mode": self.update_mode,
            "unit": self.unit,
            "direction": self.direction,
            "price_basis": self.price_basis,
            "version": self.version,
            "source_version_policy": self.source_version_policy,
            "dependencies": [item.to_payload() for item in self.dependencies],
            "warmup": self.warmup,
            "lag": self.lag,
            "recompute_anchor": (
                None if self.recompute_anchor is None else self.recompute_anchor.isoformat()
            ),
            "formula": self.formula,
            "aliases": list(self.aliases),
            "tolerance": self.tolerance.to_payload(),
            "enabled": self.enabled,
            "materialization": self.materialization.value,
        }
