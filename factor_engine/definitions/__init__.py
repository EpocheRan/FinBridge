from factors.registry import register_factor
from factors.spec import Materialization

from ..fin_ext import FIN_EXT_SPECS
from ..fundamental import FUNDAMENTAL_SPECS, FundamentalComputer
from ..valuation import VALUATION_SPECS, ValuationComputer
from .legacy import LEGACY_COLUMNS, LEGACY_SPECS, LegacyComputer, all_builtin_map
from .price import PRICE_FACTORS, BuiltinFactor, builtin_map
from .price_factors import PRICE_LIB_SPECS

#: 可逐 bar 现算的注册因子（materialization != STORED）。
INLINE_COMPUTE_REGISTRY: dict[str, BuiltinFactor] = {
    factor_id: builtin
    for factor_id, builtin in builtin_map().items()
    if builtin.spec.materialization != Materialization.STORED
}

def register_all() -> list[str]:
    """把内置 + legacy + 基本面因子全部注册进全局 registry（幂等）。"""
    ids: set[str] = set()
    for item in PRICE_FACTORS:
        register_factor(item.spec, replace=True)
        ids.add(item.spec.factor_id)
    for spec in LEGACY_SPECS:
        register_factor(spec, replace=True)
        ids.add(spec.factor_id)
    for spec in FUNDAMENTAL_SPECS:
        register_factor(spec, replace=True)
        ids.add(spec.factor_id)
    for spec in VALUATION_SPECS:
        register_factor(spec, replace=True)
        ids.add(spec.factor_id)
    for spec in PRICE_LIB_SPECS:
        register_factor(spec, replace=True)
        ids.add(spec.factor_id)
    for spec in FIN_EXT_SPECS:
        register_factor(spec, replace=True)
        ids.add(spec.factor_id)
    return sorted(ids)

__all__ = [
    "FIN_EXT_SPECS",
    "FUNDAMENTAL_SPECS",
    "FundamentalComputer",
    "INLINE_COMPUTE_REGISTRY",
    "LEGACY_COLUMNS",
    "LEGACY_SPECS",
    "PRICE_FACTORS",
    "PRICE_LIB_SPECS",
    "VALUATION_SPECS",
    "BuiltinFactor",
    "LegacyComputer",
    "ValuationComputer",
    "all_builtin_map",
    "builtin_map",
    "register_all",
]
