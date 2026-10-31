"""单因子横截面骨架：换 ``--param factor=...`` 就能扫任意已注册因子。

存在的理由：策略层面最贵的不是写策略，是**每个因子都要重写一遍策略**。
这个骨架把「战场（universe）+ 因子 + 方向 + 持仓数」全部参数化，
用一条命令就能把一个因子放到任意池子上跑出净值，作为因子取舍的最终裁判。

用法::

    python -m backtest run strategies/models/factor_rank.py --start 2020-01-01 --end 2026-10-29 \
        --param index=000905 --param factor=reversal.ret_21d --param direction=-1

    # 不带 index 则用固定 200 池
    python -m backtest run strategies/models/factor_rank.py ... --param factor=size.float_mv

方向必须显式给（族先验，不按样本内 IC 翻转）；方向错的结果不能拿来当证据。
"""

from __future__ import annotations

import pandas as pd

from factors import F, FactorDef
from strategies.base import CrossSectionStrategy
from strategies.pools.pool_2017_200 import POOL_2017_11_01_200
from strategies.universe import Rebalance, Universe


class FactorRank(CrossSectionStrategy):
    name = "factor_rank"
    universe = Universe.explicit(POOL_2017_11_01_200)
    top_n = 30
    buffer = 10
    rebalance = Rebalance.MONTHLY
    params = {
        "factor": "liquidity.turnover_std_bias_42_252",
        "direction": -1,
        "index": "",
        "top_n": 30,
        "buffer": 10,
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.top_n = int(self.params["top_n"])
        self.buffer = int(self.params["buffer"])
        index = str(self.params.get("index") or "").strip()
        if index:
            self.universe = Universe.index(index.zfill(6) if index.isdigit() else index)
        # 快速失败：因子 id 写错时在构造期报错，而不是回测跑完给一条假净值
        F.ts(str(self.params["factor"]))
        from strategies.weighting import EqualWeight

        self.weighting = EqualWeight()

    def factors(self) -> list[FactorDef]:
        return [F.ts(str(self.params["factor"])), F.vol(20)]

    def rank(self, panel, dt: pd.Timestamp) -> pd.Series:
        raw = panel.at(dt, str(self.params["factor"]))
        score = raw * int(self.params["direction"])
        return score.dropna()


STRATEGY = FactorRank
