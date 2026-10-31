"""同一因子、换成 PIT 指数成分池：验证 ``Universe.index`` 解析。

与 ``turnover_regime.py`` 的因子/参数完全相同，唯一区别是股票池来自
``index_weight`` 的逐期快照：

- 不用写死代码列表，成分随指数调整自动更新；
- 非成分股在对应交易日不参与排名（调仓日与 buffer 退出都按当日成分过滤）；
- ``run_id`` 包含成分快照指纹，指数调仓后会得到新的 run。

``--param index=000905`` 可换中证500；月内成分变动按上月末快照处理。
"""

from __future__ import annotations

from strategies.models.turnover_regime import TurnoverRegime
from strategies.universe import Universe


class IndexTurnoverRegime(TurnoverRegime):
    name = "index_turnover_regime"
    universe = Universe.index("000300")
    params = {
        **TurnoverRegime.params,
        "index": "000300",
    }

    def __init__(self, params=None):
        super().__init__(params)
        # --param index=000905 会被命令行解析成 int 905，这里补回 6 位代码
        code = str(self.params["index"]).strip()
        self.universe = Universe.index(code.zfill(6) if code.isdigit() else code)


STRATEGY = IndexTurnoverRegime
