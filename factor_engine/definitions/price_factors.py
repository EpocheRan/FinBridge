"""价量因子的**元数据层**：把 86 个算子登记成 6 类 ``FactorSpec``。

分层
----
- 计算在 ``factors/ops/price_factors.py``（纯 pandas）；
- 分类/命名/窗口/方向/公式在本文件，登记进 ``factor_registry``；
- ``factor_id`` 形如 ``momentum.return_21d``，原始  名（含大写，如 ``MACD``）
  保留在 ``aliases`` 里，便于对账。

materialization 一律 ``INLINE``：这些因子都由日线现算，不需要落长表；
需要落库做横截面研究时，用 ``F.ts(name)`` 在 ``build_panel`` 里算即可。
"""

from __future__ import annotations

from factors.naming import house_short
from factors.ops.price_factors import GROUPS
from factors.spec import FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category

#: 类别 -> Subcategory 默认值
_DEFAULT_SUB = {
    "liquidity": "turnover",
    "momentum": "return",
    "reversal": "short",
    "risk": "total_vol",
    "size": "total_mv",
}

#: 因子名 -> (subcategory, direction) 覆盖
_OVERRIDES: dict[str, tuple[str, str]] = {
    "amount_ma_20d": ("amount", "negative"),
    "sum_abs_rtn_amount_20d": ("amount", "negative"),
    "volume_alpha_300d_000001": ("impact_cost", "positive"),
    "volume_alpha_300d_000300": ("impact_cost", "positive"),
    "turnover_ma_20d": ("turnover", "negative"),
    "turnover_ma_20d_120d": ("turnover", "negative"),
    "alpha_125d_000300": ("relative_strength", "positive"),
    "alpha_250d_000300": ("relative_strength", "positive"),
    "alpha_500d_000300": ("relative_strength", "positive"),
    "alpha_1000d_000300": ("relative_strength", "positive"),
    "alpha_528d_000001": ("relative_strength", "positive"),
    "alpha_792d_000001": ("relative_strength", "positive"),
    "alpha_1320d_000001": ("relative_strength", "positive"),
    "rsrs": ("relative_strength", "positive"),
    "MACD": ("acceleration", "positive"),
    "dif": ("acceleration", "positive"),
    "dea": ("acceleration", "positive"),
    "days_down_up": ("acceleration", "positive"),
    "ma_20d": ("return", "positive"),
    "price_position_ir_60d": ("return", "positive"),
    "rsi": ("short", "negative"),
    "price_dist": ("short", "positive"),
    "days_beyond_upper_lower_21d": ("total_vol", "negative"),
    "beta_60d_000300": ("beta", "negative"),
    "beta_125d_000300": ("beta", "negative"),
    "beta_250d_000300": ("beta", "negative"),
    "beta_500d_000300": ("beta", "negative"),
    "beta_1000d_000300": ("beta", "negative"),
    "beta_1320d_000001": ("beta", "negative"),
    "volume_beta_120d_000300": ("beta", "negative"),
    "beta_consistency_1320d_000300": ("beta", "negative"),
    "sigma_1320d_000001": ("idiosyncratic_vol", "negative"),
    "sigma_1320d_000300": ("idiosyncratic_vol", "negative"),
    "log_price": ("price_level", "positive"),
    "float_size": ("float_mv", "positive"),
}

#: 各因子的口径说明（写进 registry 的 formula 字段）
_DESC = {
    "liquidity": "换手/成交额类：均值、标准差、长短期乖离、单位成交额波动",
    "momentum": "动量类：N 日收益、均线、MACD、RSRS、相对基准截距 Alpha",
    "reversal": "反转类：短期收益反转、RSI、整数关口距离",
    "risk": "风险类：波动率、高低点比、夏普、Beta、特质波动",
    "size": "规模类：总市值/流通市值负对数",
}

_SOURCE = SourceSpec("ohlcv_1d", "questdb", ("close", "volume", "amount"), "trade_date")

def _specs_for(group: str) -> tuple[FactorSpec, ...]:
    category = Category(group)
    specs: list[FactorSpec] = []
    for name, (_fn, window, unit) in GROUPS[group].items():
        subcategory, direction = _OVERRIDES.get(name, (_DEFAULT_SUB[group], "positive"))
        alias = name if name == name.lower() else name
        specs.append(
            FactorSpec(
                factor_id=f"{group}.{house_short(group, name)}",
                name=house_short(group, name),
                category=category,
                subcategory=subcategory,
                source=_SOURCE,
                window=int(window),
                freq="1d",
                value_kind="daily",
                update_mode="shift",
                unit=unit,  # type: ignore[arg-type]
                direction=direction,  # type: ignore[arg-type]
                price_basis="hfq",
                version="v1",
                source_version_policy="vendor",
                warmup=int(window),
                formula=f"src:{name}",
                aliases=(alias,),  # 旧名追溯用，不进 factor_id
                materialization=Materialization.INLINE,
            )
        )
    return tuple(specs)

LIQUIDITY_SPECS = _specs_for("liquidity")
MOMENTUM_SPECS = _specs_for("momentum")
REVERSAL_SPECS = _specs_for("reversal")
RISK_SPECS = _specs_for("risk")
SIZE_SPECS = _specs_for("size")

PRICE_LIB_SPECS: tuple[FactorSpec, ...] = (
    LIQUIDITY_SPECS + MOMENTUM_SPECS + REVERSAL_SPECS + RISK_SPECS + SIZE_SPECS
)

#: factor_id ->  原始名（回算时用）
FACTOR_ID_TO_NAME: dict[str, str] = {
    spec.factor_id: spec.aliases[0] for spec in PRICE_LIB_SPECS
}

__all__ = [
    "FACTOR_ID_TO_NAME",
    "LIQUIDITY_SPECS",
    "MOMENTUM_SPECS",
    "REVERSAL_SPECS",
    "RISK_SPECS",
    "SIZE_SPECS",
    "PRICE_LIB_SPECS",
]
