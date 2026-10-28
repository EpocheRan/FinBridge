"""因子分类体系（与截图对齐）。"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    VALUE = "value"
    QUALITY = "quality"
    GROWTH = "growth"
    MOMENTUM = "momentum"
    REVERSAL = "reversal"
    SIZE = "size"
    LIQUIDITY = "liquidity"
    RISK = "risk"
    SENTIMENT = "sentiment"
    ANALYST = "analyst"
    LEVERAGE = "leverage"
    TECHNICAL = "technical"


#: 二级目录（子维度）白名单；不在表内的子维度由注册时按 category 校验
SUBCATEGORIES: dict[Category, tuple[str, ...]] = {
    Category.VALUE: ("earnings", "asset", "income", "cash_dividend"),
    Category.QUALITY: ("profitability", "earnings_quality", "stability", "financial_safety"),
    Category.GROWTH: ("revenue", "profit", "asset_cashflow"),
    Category.MOMENTUM: ("return", "relative_strength", "acceleration"),
    Category.REVERSAL: ("short", "long"),
    Category.SIZE: ("total_mv", "float_mv", "free_float_mv", "log_mv"),
    Category.LIQUIDITY: ("turnover", "amount", "impact_cost", "depth"),
    Category.RISK: ("total_vol", "idiosyncratic_vol", "beta", "downside"),
    Category.SENTIMENT: ("moneyflow", "margin", "northbound"),
    Category.ANALYST: ("estimate", "rating", "target_price"),
    Category.LEVERAGE: ("debt", "financial", "operating"),
    Category.TECHNICAL: ("ma_deviation", "rsi", "breakout", "atr"),
}


def normalize_category(value: Category | str) -> Category:
    if isinstance(value, Category):
        return value
    try:
        return Category(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"未知因子类别: {value!r}，可选: {[item.value for item in Category]}"
        ) from exc


def normalize_subcategory(category: Category, value: str) -> str:
    text = str(value).strip().lower()
    allowed = SUBCATEGORIES.get(category, ())
    if allowed and text not in allowed:
        raise ValueError(
            f"类别 {category.value} 的子维度 {value!r} 不在白名单: {list(allowed)}"
        )
    return text
