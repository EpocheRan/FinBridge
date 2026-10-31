"""报告输入契约（B → E 的边界）。

报告层不允许自己取数。E 只接收这里装配好的 ``ReportInputs``：

- 数据来自 B（``research.eval.data.load_panel``，唯一取数入口）；
- 因子值来自 C（``research.eval.compute.factor_panel``）；
- 评估结果来自 D（``research.eval_items.execute``，由 pipeline 产出）。

``report/panel.py`` 里残留的 ``load_panel`` 属历史实现，迁移完成后应删除，
本文件是替代它的唯一入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from research.eval.data import PanelData


@dataclass
class ReportInputs:
    """渲染一份报告所需的全部输入；不含任何 IO。"""

    panel: PanelData
    factor_values: pd.DataFrame                 # date × symbol，已按方向调整的信号
    item_results: dict[str, dict[str, Any]]     # 评估项 id -> {status, values, note}
    profile: dict[str, Any]                     # F 的 profile 快照
    spec: dict[str, Any]                        # universe / start / end / horizon / cost
    domains: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def universe_label(self) -> str:
        return str(self.spec.get("universe", self.panel.label))


def assemble(
    panel: PanelData,
    table: pd.DataFrame,
    meta: dict[str, Any],
    factor_id: str,
) -> ReportInputs:
    """把 pipeline 的 (panel, table, meta) 装配成报告输入；本函数不做任何 IO。"""
    row = table[table["factor_id"] == factor_id].iloc[0].to_dict()
    results = meta.get("item_status", {}).get(factor_id, {})
    return ReportInputs(
        panel=panel,
        factor_values=pd.DataFrame(index=panel.dates, columns=panel.codes, dtype="float64"),
        item_results={key: {"status": value[0], "note": value[1]} for key, value in results.items()},
        profile=meta["profile"],
        spec={
            "universe": row.get("universe"),
            "start": row.get("start"),
            "end": row.get("end"),
            "horizon": row.get("horizon"),
        },
        domains=meta.get("domains", []),
        extras={"table": table},
    )


__all__ = ["ReportInputs", "assemble"]
