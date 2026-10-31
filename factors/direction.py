"""因子方向（族先验）的唯一事实源。

背景：``factor_registry`` 里逐条登记的 ``direction`` 与
``research/report/definitions.py`` 的族先验不一致——例如
``liquidity.turnover_*`` 在 registry 里是 ``positive``，而报告口径是 ``-1``
（短期换手放大 = 拥挤 = 未来收益低）。方向错会把整张评估表反号，
所以方向不能由各消费方各自解释，必须集中在这里。

规则：按前缀匹配，先命中先用；未命中回落到 registry 的登记值。
所有方向都是**族先验**，不使用样本内 IC 符号（避免前视/过拟合）。
"""

from __future__ import annotations

#: (前缀, 方向)；顺序即优先级
_RULES: tuple[tuple[str, int], ...] = (
    ("liquidity.turnover", -1),
    ("liquidity.avg_turnover", -1),
    ("liquidity.std_turnover", -1),
    ("liquidity.amount_ma", -1),
    ("liquidity.sum_abs_rtn_amount", -1),
    ("liquidity.volume_alpha", +1),
    ("reversal.", -1),
    ("risk.vol_", -1),
    ("risk.return_std", -1),
    ("risk.high_low", -1),
    ("risk.beta", -1),
    ("risk.ivol", -1),
    ("risk.sigma", -1),
    ("risk.volume_beta", -1),
    ("risk.sharpe", +1),
    ("risk.adjusted_sharpe", +1),
    ("momentum.", +1),
    ("size.", +1),
    ("sentiment.margin", -1),
    ("sentiment.flow_large", +1),
    ("sentiment.flow_retail", -1),
)


def direction_of(factor_id: str, default: int = 1) -> int:
    """返回 +1 / -1；未命中规则时回落到 ``default``。"""
    text = str(factor_id).strip().lower()
    for prefix, value in _RULES:
        if text.startswith(prefix):
            return value
    return int(default)


def has_rule(factor_id: str) -> bool:
    text = str(factor_id).strip().lower()
    return any(text.startswith(prefix) for prefix, _ in _RULES)


__all__ = ["direction_of", "has_rule"]
