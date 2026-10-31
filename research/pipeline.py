"""主链路：F(profile) → B(data) → C(compute) → D(items) → 统一表。

与 old ``research/eval`` 的区别：评估项由 profile 决定，D 只负责按目录执行；
新增评估项不改本文件。
"""

from __future__ import annotations

import pandas as pd

from factors.direction import direction_of
from research.eval.data import load_panel
from research.eval.metrics import forward_returns
from research.eval.compute import factor_panel as _factor_panel
from research.eval.spec import EvalSpec
from research.eval_items import EvalContext, catalog, execute, schema_for
from research.eval_items.base import ItemResult, STATUS_UNAVAILABLE
from research import profile as profile_mod

CORE: tuple[str, ...] = (
    "factor_id", "profile", "profile_version", "universe", "start", "end", "horizon",
)

_EXTRA_COLUMNS = {
    "margin_detail": ("margin_rzye", "margin_rzmre", "margin_rqye", "margin_rqyl"),
    "moneyflow_daily": ("mf_buy_sm", "mf_sell_sm", "mf_buy_lg", "mf_sell_lg",
                        "mf_buy_elg", "mf_sell_elg", "mf_net_amount"),
    "fin_valuation_daily": ("total_mv", "float_mv"),
}


def _extras_for(factor_ids: list[str]) -> tuple[str, ...]:
    from factors.ops.flow import FLOW_FACTORS

    datasets: set[str] = set()
    for factor_id in factor_ids:
        if factor_id.startswith("size."):
            datasets.add("fin_valuation_daily")
        if factor_id in FLOW_FACTORS:
            datasets.add(FLOW_FACTORS[factor_id][4])
    return tuple(sorted(datasets))


def domain_coverage(frame: pd.DataFrame) -> list[dict]:
    """每个数据域的实际覆盖区间，供报告「完整性申明」使用。"""
    out: list[dict] = []
    dates = pd.to_datetime(frame["date"])
    market = frame.loc[frame["close"].notna(), "date"]
    out.append({
        "domain": "行情与基础",
        "dataset": "ohlcv_1d",
        "start": str(pd.Timestamp(market.min()).date()) if len(market) else "-",
        "end": str(pd.Timestamp(market.max()).date()) if len(market) else "-",
        "status": "ok" if len(market) else "missing",
    })
    for dataset, columns in _EXTRA_COLUMNS.items():
        present = [c for c in columns if c in frame.columns]
        if not present:
            out.append({"domain": dataset, "dataset": dataset, "start": "-", "end": "-",
                        "status": "not_loaded"})
            continue
        mask = frame[present].notna().any(axis=1)
        covered = frame.loc[mask, "date"]
        out.append({
            "domain": dataset,
            "dataset": dataset,
            "start": str(pd.Timestamp(covered.min()).date()) if len(covered) else "-",
            "end": str(pd.Timestamp(covered.max()).date()) if len(covered) else "-",
            "status": "ok" if len(covered) else "empty",
        })
    return out


def run(
    factor_ids: list[str],
    *,
    profile: str = "default",
    universe: str = "all_a",
    start: str = "2020-01-01",
    end: str | None = None,
    horizon: int = 20,
    cost_bps: float = 10.0,
    quantile: float = 0.2,
    min_names: int = 50,
    rating_from_report: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    spec_profile = profile_mod.load(profile)
    items = list(spec_profile["items"])
    columns = CORE + schema_for(items)
    spec = EvalSpec(
        universe=universe, start=start, end=end, horizon=horizon,
        cost_bps=cost_bps, quantile=quantile, min_names=min_names,
        reference=str(spec_profile.get("reference") or ""),
        extras=_extras_for(list(factor_ids)),
    )

    panel = load_panel(spec)
    forward = forward_returns(panel, spec)

    # 评估项可能要求额外数据：多周期收益、参照因子
    extras: dict = {}
    if "decay" in items:
        close = panel.frame.pivot_table(index="date", columns="symbol", values="hfq_close")
        open_ = panel.frame.pivot_table(index="date", columns="symbol", values="hfq_open")
        extras["forward_by_horizon"] = {
            f"{h}d": close.shift(-h) / open_.shift(-1) - 1 for h in (1, 5, 10, 20)
        }
    if "reference_rho" in items and spec.reference:
        try:
            ref_values = _factor_panel(panel.frame, spec.reference, spec)
            extras["reference_signal"] = ref_values.reindex(panel.dates) * direction_of(spec.reference)
            extras["reference_id"] = spec.reference
        except Exception:  # noqa: BLE001 - 参照因子缺失只降级该项
            extras["reference_signal"] = None
    # 评级口径对齐：报告生成后把 report/rating.py 的结果传进来，不再另算一套
    extras["rating_from_report"] = rating_from_report

    rows: list[dict] = []
    item_rows: dict[str, dict] = {}
    end_label = str(panel.dates.max().date()) if len(panel.dates) else (spec.end or "")
    for factor_id in factor_ids:
        base = {
            "factor_id": factor_id,
            "profile": spec_profile["name"],
            "profile_version": spec_profile["version"],
            "universe": spec.universe,
            "start": spec.start,
            "end": end_label,
            "horizon": int(spec.horizon),
        }
        try:
            values = _factor_panel(panel.frame, factor_id, spec).reindex(panel.dates)
        except Exception as exc:  # noqa: BLE001
            base["note"] = f"因子值计算失败: {type(exc).__name__}: {exc}"
            rows.append(base)
            continue
        direction = direction_of(factor_id)
        ctx = EvalContext(
            factor_id=factor_id,
            direction=direction,
            values=values,
            signal=values * direction,
            forward=forward,
            membership=panel.membership,
            panel_dates=panel.dates,
            quantile=quantile,
            min_names=min_names,
            cost_bps=cost_bps,
            horizon=horizon,
            extras=extras,
        )
        results = execute(items, ctx, spec_profile.get("item_params") or {})
        merged = dict(base)
        notes: list[str] = []
        for result in results:
            merged.update(result.values)
            if result.status == STATUS_UNAVAILABLE:
                merged[f"{result.id}_status"] = STATUS_UNAVAILABLE
            if result.note:
                notes.append(f"{result.id}: {result.note}")
        if notes:
            merged["note"] = " | ".join(notes)
        rows.append(merged)
        item_rows[factor_id] = {r.id: (r.status, r.note) for r in results}

    ordered = list(dict.fromkeys(
        list(columns)
        + [c for c in (rows[0].keys() if rows else []) if c not in columns]
        + ["note"]
    ))
    table = pd.DataFrame(rows).reindex(columns=ordered)
    meta = {
        "profile": spec_profile,
        "items": items,
        "catalog": sorted(catalog()),
        "not_selected": [i for i in sorted(catalog()) if i not in items],
        "domains": domain_coverage(panel.frame),
        "panel": {
            "label": panel.label, "codes": len(panel.codes),
            "dates": len(panel.dates),
            "start": str(panel.dates.min().date()) if len(panel.dates) else "",
            "end": end_label,
        },
        "item_status": item_rows,
    }
    return table, meta


__all__ = ["CORE", "domain_coverage", "run"]
