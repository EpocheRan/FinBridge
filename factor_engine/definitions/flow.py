"""情绪/资金类因子的**元数据层**：把两融与资金流算子登记成 ``sentiment`` 族。

分层与价量因子一致：

- 计算在 ``factors/ops/flow.py``（纯 pandas，列缺失返回 NaN）；
- 分类/方向/公式/数据来源在本文件，登记进 ``factor_registry``；
- ``materialization=INLINE``：回测时随面板现算，不落长表。

方向全部是**族先验**（写死，不按样本内 IC 翻转）：
融资加仓/融券占比高 → 拥挤 → 负向；大单净流入 → 正向；小单接盘 → 负向。
"""

from __future__ import annotations

from factors.ops.flow import FLOW_FACTORS
from factors.spec import FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category

_SUBCATEGORY = {
    "margin_detail": "margin",
    "moneyflow_daily": "moneyflow",
}

_FORMULA = {
    "sentiment.margin_balance_chg_20d": "融资余额_t / 融资余额_(t-20) - 1",
    "sentiment.margin_short_ratio": "融券余额 / 融资余额",
    "sentiment.margin_buy_ratio_20d": "MA20(融资买入额 / 成交额)",
    "sentiment.flow_large_net_ratio_20d": "MA20((大单+特大单净买入) / 成交额)",
    "sentiment.flow_retail_net_ratio_20d": "MA20(小单净买入 / 成交额)",
}

_FIELDS = {
    "margin_detail": ("rzye", "rzmre", "rqye"),
    "moneyflow_daily": (
        "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
        "buy_sm_amount", "sell_sm_amount",
    ),
}


def _specs() -> tuple[FactorSpec, ...]:
    specs: list[FactorSpec] = []
    for factor_id, (_fn, window, unit, direction, dataset) in FLOW_FACTORS.items():
        short = factor_id.split(".", 1)[1]
        specs.append(
            FactorSpec(
                factor_id=factor_id,
                name=short,
                category=Category.SENTIMENT,
                subcategory=_SUBCATEGORY[dataset],
                source=SourceSpec(dataset, "questdb", _FIELDS[dataset]),
                window=int(window),
                freq="1d",
                value_kind="daily",
                update_mode="window" if int(window) > 1 else "shift",
                unit=unit,  # type: ignore[arg-type]
                direction="positive" if int(direction) > 0 else "negative",
                price_basis="hfq",
                version="v1",
                source_version_policy="vendor",
                warmup=int(window),
                formula=_FORMULA.get(factor_id, short),
                materialization=Materialization.INLINE,
            )
        )
    return tuple(specs)


FLOW_SPECS: tuple[FactorSpec, ...] = _specs()

#: 需要的辅助数据集（回测/研究取数用）
FLOW_DATASETS: tuple[str, ...] = tuple(sorted({spec.source.dataset for spec in FLOW_SPECS}))

__all__ = ["FLOW_DATASETS", "FLOW_SPECS"]
