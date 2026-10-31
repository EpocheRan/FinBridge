"""换手波动状态 × 两融拥挤度：把「正交因子」合成进策略。

与 ``turnover_regime.py`` 的差别只在因子组合，其余（池、调仓、权重）保持一致，
这样两次回测的差异可以归因到合成，而不是参数。

合成方式
--------
每个分量先转「信号」（原值 × 方向），再取截面百分位秩，最后等权平均：
缺失分量不填 0，只在可用分量上平均——两融标的之外（非两融/新股）仍能用冠军因子
参与排名，不会被整体剔除。

分量与方向（均为族先验，不按样本内 IC 翻转）：

- ``liquidity.turnover_std_bias_42_252``：短期换手波动放大 → 拥挤 → -1
- ``sentiment.margin_balance_chg_20d``：融资余额 20 日增速 → 杠杆加仓 → -1
- ``sentiment.margin_short_ratio``：融券/融资 → 看空占比 → -1

实测依据见 ``docs/notes/20261031_factor_idea_backlog.md``：
三者两两 ρ≈0.14~0.16，合成后 IC +22%、净价差翻倍。
"""

from __future__ import annotations

import pandas as pd

from factors import F, FactorDef
from strategies.base import CrossSectionStrategy
from strategies.models.turnover_regime import TurnoverRegime
from strategies.pools.pool_2017_200 import POOL_2017_11_01_200
from strategies.universe import Universe

CHAMPION = "liquidity.turnover_std_bias_42_252"
MARGIN_BALANCE = "sentiment.margin_balance_chg_20d"
MARGIN_SHORT = "sentiment.margin_short_ratio"

#: 分量 -> 方向先验
PARTS: dict[str, int] = {
    CHAMPION: -1,
    MARGIN_BALANCE: -1,
    MARGIN_SHORT: -1,
}


class TurnoverRegimeMargin(CrossSectionStrategy):
    name = "turnover_regime_margin"
    universe = Universe.explicit(POOL_2017_11_01_200)
    top_n = 30
    buffer = 10
    rebalance = TurnoverRegime.rebalance
    weighting = TurnoverRegime.weighting
    params = {
        "top_n": 30,
        "buffer": 10,
        "weighting": "equal",
        "parts": ",".join(PARTS),
        "index": "",
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.top_n = int(self.params["top_n"])
        self.buffer = int(self.params["buffer"])
        # 留空 = 用固定 200 池；给了指数代码则走 PIT 成分
        index = str(self.params.get("index") or "").strip()
        if index:
            self.universe = Universe.index(index.zfill(6) if index.isdigit() else index)
        if str(self.params["weighting"]).lower() in ("inverse_vol", "inv_vol"):
            from strategies.weighting import InverseVolWeight

            self.weighting = InverseVolWeight("vol20", cap=0.08)
        else:
            from strategies.weighting import EqualWeight

            self.weighting = EqualWeight()
        raw = str(self.params.get("parts") or "").strip()
        self.parts = (
            {name: PARTS[name] for name in raw.split(",") if name in PARTS}
            if raw
            else dict(PARTS)
        )

    def factors(self) -> list[FactorDef]:
        defs: list[FactorDef] = [F.ts(name) for name in self.parts]
        defs.append(F.vol(20))
        return defs

    def rank(self, panel, dt: pd.Timestamp) -> pd.Series:
        columns: dict[str, pd.Series] = {}
        for name, direction in self.parts.items():
            try:
                raw = panel.at(dt, name)
            except KeyError:
                continue
            if raw is None or raw.empty:
                continue
            signal = raw * direction
            # 先定方向再取百分位：低值好 → 信号高 → 秩高。
            columns[name] = signal.rank(pct=True)
        if not columns:
            return pd.Series(dtype="float64")
        scores = pd.DataFrame(columns).mean(axis=1, skipna=True)
        return scores.dropna()


STRATEGY = TurnoverRegimeMargin
