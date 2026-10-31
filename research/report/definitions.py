"""因子定义（人类可读）与增量性对照组。

报告的第一页必须回答「这个因子到底是什么」：原始字段、窗口、单位、是否取对数/
是否除以市值、方向如何定义。这些信息此前只存在于 ``factor_engine`` 的
``formula="src:xxx"`` 里，对读者不可见。本模块把 registry 的元数据翻译成
可直接写进报告的表格，并为增量性检验提供同类因子清单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_ECON = {
    "liquidity": "流动性/关注度代理：成交越活跃，定价越充分，未来超额收益通常越低。",
    "momentum": "动量/趋势延续：过去强势的股票短期内继续跑赢。",
    "reversal": "短期反转：前期涨跌过度，随后均值回归。",
    "risk": "风险溢价/低波异象：低风险股票风险调整后收益更高。",
    "size": "规模效应：小市值股票长期存在超额收益。",
}


@dataclass(frozen=True)
class Definition:
    factor_id: str
    name: str
    category: str
    formula: str
    source: str
    unit: str
    transform: str
    window: int
    direction: int
    direction_text: str
    economic: str
    notes: str = ""
    direction_source: str = "registry"
    pit: str = ""
    lag: int = 0

    def as_dict(self) -> dict:
        return {
            "factor_id": self.factor_id,
            "name": self.name,
            "category": self.category,
            "formula": self.formula,
            "source": self.source,
            "unit": self.unit,
            "transform": self.transform,
            "window": self.window,
            "direction": self.direction,
            "direction_text": self.direction_text,
            "economic": self.economic,
            "notes": self.notes,
            "direction_source": self.direction_source,
            "pit": self.pit,
            "lag": self.lag,
        }


_UNIT_TEXT = {
    "currency": "元",
    "pct": "%",
    "ratio": "比值（无量纲）",
    "price": "元/股",
    "days": "日",
}


def _direction_text(direction: int, category: str) -> str:
    """唯一方向措辞：因子值本身不取负，评估时统一乘 ``direction``。"""
    if direction < 0:
        return "反向（评估时 ×−1；因子值越小，信号越强）"
    return "正向（评估时 ×+1；因子值越大，信号越强）"


def _registry_meta(factor_id: str) -> tuple[int, int, str, int, str, str]:
    """从 registry 取 ``(window, direction, unit, lag, as_of, formula)``。

    价量、财务扩展、估值/成长三套 registry 都要查；此前只查价量，导致
    quality/value 一律退化成 ``direction=+1``，杠杆类因子被系统性反向。
    """
    try:
        from factor_engine.definitions.price_factors import PRICE_LIB_SPECS

        specs = list(PRICE_LIB_SPECS)
    except Exception:  # noqa: BLE001
        specs = []
    try:
        from factor_engine.fin_ext import FIN_EXT_SPECS

        specs += list(FIN_EXT_SPECS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from factor_engine.fundamental import FUNDAMENTAL_SPECS

        specs += list(FUNDAMENTAL_SPECS)
    except Exception:  # noqa: BLE001
        pass
    for spec in specs:
        if spec.factor_id == factor_id:
            direction = -1 if str(getattr(spec, "direction", "positive")) == "negative" else 1
            as_of = str(getattr(getattr(spec, "source", None), "as_of", "") or "")
            formula = str(getattr(spec, "formula", "") or "")
            return (
                int(getattr(spec, "window", 0) or 0),
                direction,
                str(getattr(spec, "unit", "ratio")),
                int(getattr(spec, "lag", 0) or 0),
                as_of,
                formula,
            )
    return 0, 1, "ratio", 0, "", ""


def describe(factor_id: str) -> Definition:
    """返回因子的可读定义；未知因子退化为「按名称解析」。"""
    key = str(factor_id)
    category, _, short = key.partition(".")
    window, direction, unit, lag, as_of, reg_formula = _registry_meta(key)
    econ = DEFAULT_ECON.get(category, "见因子注册表。")
    transform = "原值（未取对数 / 未除以市值）"
    notes = ""
    pit = ""
    if category in ("quality", "value", "growth", "valuation"):
        pit = (
            f"财务字段按公告日（publish_date）对齐，``as_of={as_of or 'available_at'}``；"
            f"额外滞后期 lag={lag} 个交易日；重述只保留最近一次入库值，历史重述未逐期回溯。"
            "因此 quality/value 的结论应读作「在已披露信息下的上限」，不是严格 PIT 复现。"
        )

    m = re.fullmatch(r"amount_ma_(\d+)d", short)
    if category == "liquidity" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"MA{n}(日成交额) = 过去 {n} 个交易日成交额的算术平均",
            source="ohlcv_1d.amount（当日成交金额，元，不复权）",
            unit="元", transform="原值；评估时按 direction=−1 反向（低成交额为正信号）",
            window=n, direction=-1,
            direction_text=_direction_text(-1, category),
            economic="成交额是流动性与市场关注度的直接代理。成交额越高，股票被充分定价、"
                     "交易拥挤度越高，未来超额收益通常越低（流动性溢价/关注度溢价）。"
                     "反向使用意味着「买低成交额」而非「买高成交额」。",
            notes="量纲随股价与股本变化，横截面比较前建议缩尾；本报告 IC 用秩相关，"
                  "对单调变换不敏感，但分层组合的极值敏感性需单独检查。",
        )

    m = re.fullmatch(r"turnover_ma_(\d+)d", short)
    if category == "liquidity" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"MA{n}(换手率)；换手率 = 成交量 / 流通股本",
            source="ohlcv_1d.turnover（%）或 volume / float_share",
            unit="%", transform="原值；评估时按 direction=−1 反向（低换手为正信号）",
            window=n, direction=-1, direction_text=_direction_text(-1, category),
            economic="换手率衡量筹码交换速度；低换手往往对应低关注度与流动性折价。",
        )

    m = re.fullmatch(r"turnover_std_(\d+)d", short)
    if category == "liquidity" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"STD_{n}(换手率)",
            source="ohlcv_1d.turnover（成交量 / 流通股本）",
            unit="%", transform="原值；评估时按 direction=−1 反向（换手波动越高，未来收益越低）",
            window=n, direction=-1, direction_text=_direction_text(-1, category),
            economic="换手率波动衡量交易活跃度的不稳定性；高波动常伴随情绪与拥挤，"
                     "后续收益偏低。该族统一按反向使用。",
            direction_source="family-prior",
        )

    m = re.fullmatch(r"(turnover_(?:std_)?bias)_(\d+)_(\d+)", short)
    if category == "liquidity" and m:
        kind, fast, slow = m.group(1), int(m.group(2)), int(m.group(3))
        label = "换手率波动偏离" if "std" in kind else "换手率偏离"
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"{label} = MA{fast}(换手率) / MA{slow}(换手率)",
            source="ohlcv_1d.turnover（成交量 / 流通股本）",
            unit="比值", transform="原值；评估时按 direction=−1 反向（偏离越高，未来收益越低）",
            window=slow, direction=-1,
            direction_text=_direction_text(-1, category),
            economic="短期换手相对长期换手的偏离衡量交易拥挤度：短期异常放大往往对应情绪高点，"
                     "随后收益回落。该族统一按反向使用，不再由样本内 IC 符号决定方向。",
            notes="分子/分母窗口高度共线，42/252 与 21/504 等变体应视为同族，不独立给权重。",
            direction_source="family-prior",
        )

    m = re.fullmatch(r"(?:return|ret)_(\d+)d", short)
    if category in ("momentum", "reversal") and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"收盘价_t / 收盘价_(t−{n}) − 1",
            source="ohlcv_1d.close（后复权）",
            unit="%", transform="收益率原值" + ("；评估时按 direction=−1 反向" if category == "reversal" else ""),
            window=n, direction=-1 if category == "reversal" else 1,
            direction_text=_direction_text(-1 if category == "reversal" else 1, category),
            economic=econ,
        )

    m = re.fullmatch(r"(?:return_std|vol)_(\d+)d", short)
    if category == "risk" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"STD_{n}日(日收益率)，年化前原值",
            source="ohlcv_1d.close（后复权）",
            unit="比值（日波动率）", transform="原值",
            window=n, direction=-1, direction_text=_direction_text(-1, category),
            economic=econ,
        )

    m = re.fullmatch(r"beta_(\d+)d_(?:csi300|sh|\d+)", short)
    if category == "risk" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"ROLLING_BETA({n} 日，相对基准 {short.split('_')[-1]})",
            source="ohlcv_1d.close + 指数收盘",
            unit="比值", transform="原值",
            window=n, direction=-1, direction_text=_direction_text(-1, category),
            economic=econ,
        )

    m = re.fullmatch(r"high_low_(\d+)d", short)
    if category == "risk" and m:
        n = int(m.group(1))
        return Definition(
            factor_id=key, name=short, category=category,
            formula=f"MAX({n}日最高价) / MIN({n}日最低价)",
            source="ohlcv_1d.high / low（后复权）",
            unit="比值", transform="原值",
            window=n, direction=-1, direction_text=_direction_text(-1, category),
            economic=econ,
        )

    if category in ("quality", "value", "growth", "valuation"):
        return Definition(
            factor_id=key, name=short or key, category=category,
            formula=f"{short}（registry 公式：{reg_formula or key}）",
            source="factor_value_daily（fin_* 报表 + fin_valuation_daily，按公告日物化）",
            unit=_UNIT_TEXT.get(unit, unit), transform=transform,
            window=window, direction=direction,
            direction_text=_direction_text(direction, category),
            economic=econ, notes=notes, pit=pit, lag=lag,
        )

    # 通用退化：至少把窗口、方向、单位讲清楚
    return Definition(
        factor_id=key, name=short or key, category=category or "unknown",
        formula=f"见 registry：{key}（窗口 {window or '—'} 日）",
        source="ohlcv_1d / factor_value_daily",
        unit=_UNIT_TEXT.get(unit, unit), transform=transform,
        window=window, direction=direction,
        direction_text=_direction_text(direction, category),
        economic=econ,
        notes="该因子尚未收录人工公式说明，报告仅展示 registry 元数据。",
    )


def sign_days(n: int) -> str:
    return f"({n} 日)"


#: 增量性对照组：同类别、经济含义接近的因子 + 规模风格
PEERS: dict[str, tuple[str, ...]] = {
    "liquidity": (
        "liquidity.turnover_ma_20d",
        "liquidity.turnover_to_mv_20_120",
        "liquidity.absret_amount_20d",
        "liquidity.turnover_ma_63d",
        "liquidity.turnover_std_21d",
        "liquidity.turnover_bias_21_252",
    ),
    "momentum": (
        "momentum.ret_21d",
        "momentum.ret_63d",
        "momentum.ret_126d",
        "momentum.alpha_125d_csi300",
    ),
    "reversal": (
        "reversal.ret_21d",
        "reversal.rsi_14d",
        "reversal.round_gap",
    ),
    "risk": (
        "risk.vol_21d",
        "risk.vol_63d",
        "risk.high_low_21d",
        "risk.beta_250d_csi300",
    ),
    "quality": (
        "quality.roe_ttm",
        "quality.roa_ttm",
        "quality.gross_margin_ttm",
        "quality.debt_to_asset",
    ),
    "value": (
        "value.ep",
        "value.bp",
        "value.sp",
        "value.fcf_p",
    ),
    "size": ("size.float_mv",),
}


def peers_for(factor_id: str) -> tuple[str, ...]:
    category = str(factor_id).partition(".")[0]
    return tuple(p for p in PEERS.get(category, ()) if p != factor_id)
