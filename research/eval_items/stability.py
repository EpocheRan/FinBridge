"""示例评估项：分年度 IC 稳定性。

这个文件存在的意义是验证「只加 G」——新增一项评估只需要：
1. 在本文件里写 ``@item(...)`` + compute()；
2. 在 ``__init__`` 里 import 一次。
执行器、pipeline、CLI、CSV schema、报告都会自动带上它，不需要改任何其他代码。
"""

from __future__ import annotations

import numpy as np

from research.eval.metrics import _mask_membership, rank_ic

from .base import STATUS_DEGRADED, STATUS_OK, EvalContext, ItemResult, ItemSpec, item


@item(
    ItemSpec(
        id="stability_by_year",
        title="分年度 IC 稳定性",
        requires=("ic",),
        outputs=("ic_positive_years", "ic_year_min"),
        artifacts=("ic_by_year",),
    )
)
def stability_by_year(ctx: EvalContext, params: dict) -> ItemResult:
    series = rank_ic(_mask_membership(ctx.signal, ctx.membership), ctx.forward, ctx.min_names)
    series = series.dropna()
    if series.empty:
        return ItemResult.unavailable("stability_by_year", "无有效 IC 样本")
    by_year = series.groupby(series.index.year).mean()
    positive = float((by_year > 0).mean())
    worst = float(by_year.min())
    status = STATUS_OK if positive >= 0.6 else STATUS_DEGRADED
    return ItemResult(
        id="stability_by_year",
        values={"ic_positive_years": positive, "ic_year_min": worst},
        artifacts={"ic_by_year": {int(k): float(v) for k, v in by_year.items()}},
        status=status,
        note="" if status == STATUS_OK else f"只有 {positive:.0%} 年份 IC 为正",
    )
