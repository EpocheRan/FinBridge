"""回测配置。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .domain.errors import ConfigError
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
    risk_free_rate: float = 0.0
    drift_threshold: float = 0.02
    seed: int = 0
    corporate_actions: CorporateActionMode = "off"
    rules: ExecutionRules = field(default_factory=ExecutionRules)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "BacktestSettings":
        payload = dict(payload or {})
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ConfigError(f"backtest 配置包含未知字段: {unknown}")

        outputs = payload.pop("outputs", ("console",))
        if isinstance(outputs, str):
            outputs = tuple(part.strip() for part in outputs.split(",") if part.strip())
        outputs = tuple(str(item) for item in outputs)
        if not outputs:
            raise ConfigError("backtest.outputs 不能为空")
        output_dir = Path(str(payload.pop("output_dir", "output/backtest")))
        rules = ExecutionRules.from_dict(payload.pop("rules", None))

        fill = str(payload.get("fill", "next_open")).strip().lower()
        if fill not in ("next_open", "close"):
            raise ConfigError(f"backtest.fill 只能是 next_open/close，收到 {fill!r}")
        source = str(payload.get("factor_source", "auto")).strip().lower()
        if source not in ("auto", "compute", "stored"):
            raise ConfigError(
                f"backtest.factor_source 只能是 auto/compute/stored，收到 {source!r}"
            )
        actions = str(payload.get("corporate_actions", "off")).strip().lower()
        if actions not in ("off", "approx"):
            raise ConfigError(
                f"backtest.corporate_actions 只能是 off/approx，收到 {actions!r}"
            )
        exposure = float(payload.get("max_gross_exposure", 0.98))
        if not 0.0 < exposure <= 1.0:
            raise ConfigError(f"max_gross_exposure 必须在 (0, 1]，收到 {exposure}")
        warmup = int(payload.get("warmup_min_bars", 260))
        if warmup < 0:
            raise ConfigError(f"warmup_min_bars 不能为负，收到 {warmup}")

        payload.update(fill=fill, factor_source=source, corporate_actions=actions)
        return cls(
            outputs=outputs,
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
