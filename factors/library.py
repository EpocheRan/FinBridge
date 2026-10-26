"""因子定义与命名空间。

因子是纯声明：策略只写 ``F.mom(20)``，不碰数据、不碰回测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

Unit = Literal[
    "ratio",
    "pct",
    "price",
    "price_delta",
    "currency",
    "shares",
    "days",
    "bool",
]
PriceBasis = Literal["raw", "qfq", "hfq", "none"]
Source = Literal["auto", "stored", "compute"]

_STORED_UNITS = {"ratio", "pct", "bool", "shares", "days", "currency"}


@dataclass(frozen=True)
class FactorDef:
    name: str
    op: str
    args: tuple[Any, ...] = ()
    inputs: tuple[str, ...] = ()
    on: str = "hfq_close"
    window: int = 1
    warmup: int = 0
    unit: Unit = "ratio"
    source: Source = "auto"
    stored_column: str | None = None
    version: str = "v1"
    price_basis: PriceBasis = "hfq"

    def __post_init__(self) -> None:
        object.__setattr__(self, "window", max(0, int(self.window)))
        object.__setattr__(self, "warmup", max(0, int(self.warmup or self.window)))
        if self.source == "stored":
            if self.unit in ("price", "price_delta"):
                raise ValueError(
                    f"factor {self.name!r}: price/price_delta 单位禁止 stored "
                    "(qfq 与 hfq 相差 per-symbol 常数)"
                )
            if not self.stored_column:
                object.__setattr__(self, "stored_column", self.name)
            object.__setattr__(self, "on", "none")
            object.__setattr__(self, "price_basis", "raw" if self.unit in ("currency", "shares") else "none")
        if self.source == "compute" and self.on == "none" and self.op not in ("rank", "zscore"):
            raise ValueError(f"factor {self.name!r}: compute 因子必须声明 on")

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "op": self.op,
            "args": list(self.args),
            "inputs": list(self.inputs),
            "on": self.on,
            "window": self.window,
            "warmup": self.warmup,
            "unit": self.unit,
            "source": self.source,
            "stored_column": self.stored_column,
            "version": self.version,
            "price_basis": self.price_basis,
        }


# 自定义因子注册表：@factor 注册后可用 F.custom(...) 或 FactorDef(op=name)
CUSTOM_FACTORS: dict[str, Callable[..., Any]] = {}


def factor(
    name: str,
    *,
    unit: Unit = "ratio",
    window: int = 1,
    warmup: int | None = None,
    inputs: tuple[str, ...] = (),
    on: str = "hfq_close",
):
    """注册自定义因子函数（输入为 Series/DataFrame，输出为 Series）。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        CUSTOM_FACTORS[name] = fn
        fn.factor_name = name  # type: ignore[attr-defined]
        fn.factor_unit = unit  # type: ignore[attr-defined]
        fn.factor_window = window  # type: ignore[attr-defined]
        fn.factor_warmup = warmup if warmup is not None else window  # type: ignore[attr-defined]
        fn.factor_inputs = inputs  # type: ignore[attr-defined]
        fn.factor_on = on  # type: ignore[attr-defined]
        return fn

    return decorator


class F:
    """策略侧因子命名空间。"""

    @staticmethod
    def mom(n: int = 20, *, on: str = "hfq_close", name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"mom{int(n)}",
            op="mom",
            args=(int(n),),
            inputs=(on,),
            on=on,
            window=int(n),
            warmup=int(n),
            unit="ratio",
            source="compute",
            price_basis="hfq",
        )

    @staticmethod
    def ma(n: int = 20, *, on: str = "hfq_close", name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"ma{int(n)}",
            op="ma",
            args=(int(n),),
            inputs=(on,),
            on=on,
            window=int(n),
            warmup=int(n),
            unit="price",
            source="compute",
            price_basis="hfq",
        )

    @staticmethod
    def atr(n: int = 14, *, name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"atr{int(n)}",
            op="atr",
            args=(int(n),),
            inputs=("high", "low", "hfq_close"),
            on="hfq_close",
            window=int(n),
            warmup=int(n),
            unit="price_delta",
            source="compute",
            price_basis="hfq",
        )

    @staticmethod
    def vol(n: int = 20, *, on: str = "hfq_close", name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"vol{int(n)}",
            op="vol",
            args=(int(n),),
            inputs=(on,),
            on=on,
            window=int(n),
            warmup=int(n),
            unit="ratio",
            source="compute",
            price_basis="hfq",
        )

    @staticmethod
    def stored(
        column: str,
        *,
        unit: Unit = "ratio",
        version: str = "v1",
        name: str | None = None,
    ) -> FactorDef:
        return FactorDef(
            name=name or column,
            op="stored",
            args=(column,),
            inputs=(),
            on="none",
            window=0,
            warmup=0,
            unit=unit,
            source="stored",
            stored_column=column,
            version=version,
            price_basis="none",
        )

    @staticmethod
    def rank(factor_name: str, *, name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"rank_{factor_name}",
            op="rank",
            args=(factor_name,),
            inputs=(factor_name,),
            on="none",
            window=0,
            warmup=0,
            unit="ratio",
            source="compute",
            price_basis="none",
        )

    @staticmethod
    def zscore(factor_name: str, *, name: str | None = None) -> FactorDef:
        return FactorDef(
            name=name or f"z_{factor_name}",
            op="zscore",
            args=(factor_name,),
            inputs=(factor_name,),
            on="none",
            window=0,
            warmup=0,
            unit="ratio",
            source="compute",
            price_basis="none",
        )

    @staticmethod
    def custom(
        op: str,
        *args: Any,
        name: str | None = None,
        unit: Unit = "ratio",
        window: int = 1,
        warmup: int | None = None,
        on: str = "hfq_close",
    ) -> FactorDef:
        return FactorDef(
            name=name or op,
            op=op,
            args=tuple(args),
            inputs=(on,),
            on=on,
            window=window,
            warmup=warmup if warmup is not None else window,
            unit=unit,
            source="compute",
            price_basis="hfq",
        )
