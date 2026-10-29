"""因子定义与命名空间。

因子是纯声明：策略只写 ``F.mom(20)``，不碰数据、不碰回测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

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

#: 横截面算子：这些 op 在“宽表”阶段执行，不需要单标的时序输入
CROSS_SECTION_OPS = ("rank", "zscore", "composite", "neutralize")

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
    materialization: str = "auto"

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
        if self.source == "compute" and self.on == "none" and self.op not in CROSS_SECTION_OPS:
            raise ValueError(f"factor {self.name!r}: compute 因子必须声明 on")
        materialization = str(self.materialization).strip().lower()
        if materialization not in ("stored", "inline", "auto"):
            raise ValueError(f"factor {self.name!r}: 未知 materialization {self.materialization!r}")
        object.__setattr__(self, "materialization", materialization)

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
            "materialization": self.materialization,
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
    def factor(
        factor_id: str,
        *,
        window: int | None = None,
        unit: Unit = "ratio",
        version: str = "v1",
        name: str | None = None,
        materialization: str = "auto",
    ) -> FactorDef:
        """读取因子库长表（F.factor）；与 legacy 的 F.stored 区分。

        ``materialization``：stored=必须读预计算 / inline=逐 bar 现算 /
        auto=stored 优先，缺失回退 inline。
        """
        return FactorDef(
            name=name or factor_id,
            op="factor",
            args=(factor_id,),
            inputs=(),
            on="none",
            window=int(window or 0),
            warmup=int(window or 0),
            unit=unit,
            source="stored",
            stored_column=factor_id,
            version=version,
            price_basis="none",
            materialization=materialization,
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
    def composite(
        parts: Sequence[Any],
        *,
        name: str = "composite",
        winsorize: float | None = None,
        neutralize: Sequence[str] | None = None,
        min_parts: int = 1,
        version: str = "v1",
    ) -> FactorDef:
        """等权/加权 **rank** 合成（横截面）。

        ``parts`` 支持三种写法：

        - ``"mom20"``：正向、权重 1
        - ``("mom20", 2.0)``：正向、权重 2
        - ``("mom20", -1, 2.0)``：负向（越小越好）、权重 2

        三个必须补齐的细节都固化在这里：

        1. **方向统一** —— 分量先转百分位秩，负向取 ``1 - rank``；
        2. **中性化** —— ``neutralize=("industry", "size")`` 时，先在 ``build_panel``
           传入的参照面板上做截面回归取残差，再排名；
        3. **缺失值** —— 不填 0：只在可用分量上重新归一权重，可用分量数
           < ``min_parts`` 时输出 ``NaN``。
        """
        specs: list[tuple[str, int, float]] = []
        for item in parts:
            if isinstance(item, str):
                specs.append((item, 1, 1.0))
                continue
            if not isinstance(item, (tuple, list)) or not item:
                raise ValueError(f"composite 分量格式错误: {item!r}")
            if len(item) == 2:
                specs.append((str(item[0]), 1, float(item[1])))
            elif len(item) == 3:
                specs.append((str(item[0]), int(item[1]), float(item[2])))
            else:
                raise ValueError(f"composite 分量格式错误: {item!r}")
        if not specs:
            raise ValueError("composite 至少需要一个分量")
        if min_parts < 1:
            raise ValueError("min_parts 必须 >= 1")
        if winsorize is not None and not (0 < float(winsorize) < 0.5):
            raise ValueError("winsorize 必须在 (0, 0.5)")

        spec = (
            tuple(specs),
            None if winsorize is None else float(winsorize),
            tuple(str(x) for x in (neutralize or ())),
            int(min_parts),
        )
        return FactorDef(
            name=name,
            op="composite",
            args=(spec,),
            inputs=tuple(item[0] for item in specs),
            on="none",
            window=0,
            warmup=0,
            unit="ratio",
            source="compute",
            price_basis="none",
            version=version,
        )

    @staticmethod
    def neutralize(
        factor_name: str,
        *,
        by: Sequence[str] = ("industry", "size"),
        name: str | None = None,
    ) -> FactorDef:
        """单因子中性化（研究用）：对指定分量做行业/市值回归取残差。"""
        spec = (str(factor_name), tuple(str(x) for x in by))
        return FactorDef(
            name=name or f"neu_{factor_name}",
            op="neutralize",
            args=(spec,),
            inputs=(str(factor_name),),
            on="none",
            window=0,
            warmup=0,
            unit="ratio",
            source="compute",
            price_basis="none",
        )

    @staticmethod
    def ts(name: str) -> FactorDef:
        """引用价量因子库中的一个因子（见 ``factors.ops.price_factors``）。"""
        from .ops.price_factors import lookup

        try:
            _fn, window, unit = lookup(name)
        except KeyError as exc:
            raise ValueError(f"未知  价量因子: {name!r}") from exc
        return FactorDef(
            name=name,
            op=name,
            args=(),
            inputs=("hfq_close",),
            on="hfq_close",
            window=int(window),
            warmup=int(window),
            unit=unit,
            source="compute",
            price_basis="hfq",
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
