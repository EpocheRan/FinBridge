from __future__ import annotations

import pandas as pd

from factors import F
from strategies.base import CrossSectionStrategy
from strategies.universe import Rebalance, Universe, UniverseSpec
from strategies.weighting import EqualWeight


class RankMom20(CrossSectionStrategy):
    name = "rank_mom20"
    # MVP 先用显式股票池跑通；后续接 all_a/as-of 成分。
    universe = Universe.explicit(
        ["600519", "000001", "300750", "600036", "000858", "601318"]
    )
    top_n = 3
    buffer = 1
    rebalance = Rebalance.WEEKLY
    weighting = EqualWeight()
    params = {"n": 20}

    def factors(self):
        return [F.mom(int(self.params["n"]), on="hfq_close")]

    def rank(self, panel, dt):
        return panel.at(dt, f"mom{int(self.params['n'])}")


STRATEGY = RankMom20
