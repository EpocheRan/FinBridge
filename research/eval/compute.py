"""因子值计算（模块 C）：factor_id → date×symbol 宽表。

从旧 ``research/eval/runner.py`` 抽出，只保留 pipeline 真正用到的部分：
预计算（STORED）因子不在本链路内，遇到直接报错并指向回测/报告流程。
"""

from __future__ import annotations

import pandas as pd

from factors import F, build_panel
from factors.registry import get as registry_get

from .spec import EvalSpec


def _definition(factor_id: str):
    try:
        spec = registry_get(factor_id)
    except Exception:  # noqa: BLE001 - 未注册（自定义算子）时退回 F.ts 的报错
        return F.ts(factor_id)
    materialization = str(getattr(getattr(spec, "materialization", ""), "value", ""))
    if materialization == "stored":
        raise ValueError(
            f"{factor_id} 是 STORED 因子（读 factor_value_daily），"
            "本链路只算面板现算因子；财务/估值因子请走报告或回测流程"
        )
    return F.ts(factor_id)


def factor_panel(frame: pd.DataFrame, factor_id: str, spec: EvalSpec) -> pd.DataFrame:
    defn = _definition(factor_id)
    end = spec.end or str(pd.to_datetime(frame["date"]).max().date())
    built = build_panel(frame, [defn], spec.start, end)
    return built.values[defn.name].sort_index()


__all__ = ["factor_panel"]
