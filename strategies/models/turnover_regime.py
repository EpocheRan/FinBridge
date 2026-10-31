"""换手波动状态因子（库内冠军）的横截面策略：基线版本。

为什么先做这个
--------------
`liquidity.turnover_std_bias_42_252` 是全库唯一稳定 B 级因子（IC 0.032、
净 +0.172%/期、年化 +7.8%、Sharpe 0.65），也是唯一有「方向先验 + 可交易价差」
双重证据的因子。在它上面跑出第一条可复现净值，比继续挖第 87 个价量因子更有用：
有了基线，后面每个新因子才有「增量」可谈。

因子
----
``std_turnover(42) / std_turnover(252) - 1``：短期换手波动相对长期基线放大。
方向先验 **-1**（拥挤 → 未来收益低），所以 ``rank`` 里取负号，让「分数越高越买」。

参数
----
- ``top_n`` / ``buffer``：持仓数与缓冲带（缓冲减少换手）
- ``weighting``：``equal`` 或 ``inverse_vol``（后者按 vol20 倒数加权）
- ``factor``：换成任意已注册价量因子即可复用本骨架

已知边界
--------
- 股票池固定为 2017-11-01 成交额前 200 只（回测层只支持 explicit universe），
  存在幸存者偏差与风格漂移；
- 因子方向按族先验固定为 -1，不按样本内 IC 翻转（与 `research.report` 口径一致）。
"""

from __future__ import annotations

import pandas as pd

from factors import F
from strategies.base import CrossSectionStrategy
from strategies.pools.pool_2017_200 import POOL_2017_11_01_200
from strategies.universe import Rebalance, Universe, UniverseSpec
from strategies.weighting import EqualWeight, InverseVolWeight

CHAMPION = "liquidity.turnover_std_bias_42_252"


class TurnoverRegime(CrossSectionStrategy):
    name = "turnover_regime"
    universe: UniverseSpec = Universe.explicit(POOL_2017_11_01_200)
    top_n = 30
    buffer = 10
    rebalance = Rebalance.MONTHLY
    weighting = EqualWeight()
    params = {
        "top_n": 30,
        "buffer": 10,
        "weighting": "equal",
        "factor": CHAMPION,
        "direction": -1,
    }

    def __init__(self, params=None):
        super().__init__(params)
        # 让 --param top_n=20 / optimize --grid top_n=20,30 真正生效：
        # 基类只合并 self.params，不会同步到类属性。
        self.top_n = int(self.params["top_n"])
        self.buffer = int(self.params["buffer"])
        if str(self.params["weighting"]).lower() in ("inverse_vol", "inv_vol"):
            self.weighting = InverseVolWeight("vol20", cap=0.08)
        else:
            self.weighting = EqualWeight()

    def factors(self):
        return [F.ts(str(self.params["factor"])), F.vol(20)]

    def rank(self, panel, dt: pd.Timestamp) -> pd.Series:
        raw = panel.at(dt, str(self.params["factor"]))
        signal = raw * int(self.params["direction"])
        # 缺失值不参与排序；框架按下标取前 top_n。
        return signal.dropna()


STRATEGY = TurnoverRegime
