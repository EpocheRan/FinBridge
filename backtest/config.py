"""回测配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .domain.models import CorporateActionMode, ExecutionRules, FillMode


@dataclass(frozen=True)
class BacktestSettings:
    engine: str = "pybroker"
    data: str = "questdb"
    factor_source: str = "auto"
    outputs: tuple[str, ...] = ("console",)
    output_dir: Path = Path("output/backtest")
    initial_cash: float = 1_000_000.0
    warmup_min_bars: int = 260
    fill: FillMode = "next_open"
    benchmark: str | None = "000300"
    rebalance: str = "weekly"
    max_gross_exposure: float = 0.98
    allow_short: bool = False
    drift_threshold: float = 0.02
    seed: int = 0
    corporate_actions: CorporateActionMode = "off"
    rules: ExecutionRules = field(default_factory=ExecutionRules)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "BacktestSettings":
        payload = dict(payload or {})
        outputs = payload.pop("outputs", ("console",))
        if isinstance(outputs, str):
            outputs = tuple(part.strip() for part in outputs.split(",") if part.strip())
        output_dir = Path(str(payload.pop("output_dir", "output/backtest")))
        rules = ExecutionRules.from_dict(payload.pop("rules", None))
        return cls(
            outputs=tuple(outputs),
            output_dir=output_dir,
            rules=rules,
            **payload,
        )

    def warmup_for(self, factors) -> int:
        """按因子声明计算 warmup。

        - 只有存在 computed 因子时才应用全局 floor；
        - stored 因子的 ``window=0``，不会因为 floor 白拉 260 天。
        """
        computed = [
            int(getattr(item, "warmup", 0) or getattr(item, "window", 0) or 0)
            for item in factors
            if getattr(item, "source", "compute") != "stored"
        ]
        if not computed:
            return 0
        return max(max(computed), int(self.warmup_min_bars))
