#!/usr/bin/env python
"""因子相关矩阵与家族聚类（解决「20d 与 21d 各算一个独立因子」）。

用法::

    python scripts/factor_corr.py --pool 200 --start 2019-01-01 --sample-dates 30
    python scripts/factor_corr.py --source financial --start 2019-01-01

输出：``factor_corr.csv``（全矩阵）、``family_corr.csv``（家族均值）、
``high_corr_pairs.csv``（|ρ|≥阈值）、``corr_report.html``（家族热力图）、``clusters.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research.report import diagnostics as diag  # noqa: E402
from research.report.panel import financial_factor_panel, load_panel, price_factor_panel  # noqa: E402

CSS = """
:root{--ink:#1a1f28;--muted:#6b7280;--rule:#d9dee4;--navy:#1f3a5f;--burgundy:#8c2f39;--wash:#f6f7f9}
*{box-sizing:border-box}body{margin:0;background:#eceef1;color:var(--ink);font-family:"Microsoft YaHei",sans-serif;font-size:12px}
.page{max-width:1200px;margin:28px auto;background:#fff;padding:36px 42px;box-shadow:0 2px 24px rgba(20,30,45,.08)}
h1{font-family:Georgia,"SimSun",serif;font-size:28px;margin:0 0 6px}h2{font-family:Georgia,"SimSun",serif;font-size:19px;margin:26px 0 8px;border-bottom:1px solid var(--rule);padding-bottom:6px}
.sub{color:var(--muted);margin-bottom:16px}.small{font-size:11px;color:var(--muted)}
table{border-collapse:collapse}td,th{padding:3px 5px;border:1px solid #eef1f4;text-align:center;font-size:10.5px}
th{color:var(--muted);font-weight:600;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.lab{text-align:left;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
"""


def _load_ids(source: str) -> list[str]:
    if source == "financial":
        from factor_engine.fin_ext import FIN_EXT_SPECS

        return [s.factor_id for s in FIN_EXT_SPECS]
    from factors.ops.price_factors import ALL_NAMES

    return [i for i in ALL_NAMES if not i.endswith(".nl_mv")]


def _panel_for(fid: str, panel, source: str) -> pd.DataFrame:
    if source == "financial" or fid.split(".", 1)[0] in ("quality", "value"):
        return financial_factor_panel(
            fid, panel.close, str(panel.close.index.min().date()), str(panel.close.index.max().date())
        )
    return price_factor_panel(fid, panel)


def compute_corr(panel, ids: list[str], source: str, sample_dates: int) -> pd.DataFrame:
    """逐日截面 Spearman 后对日期取均值；缺失值成对剔除。"""
    dates = list(panel.close.index)
    step = max(1, len(dates) // max(1, sample_dates))
    sample = dates[::step][:sample_dates]
    frames: dict[str, pd.DataFrame] = {}
    for fid in ids:
        try:
            frame = _panel_for(fid, panel, source).reindex(sample)
        except Exception:  # noqa: BLE001
            continue
        if frame.notna().sum().sum() >= 300:
            frames[fid] = frame
    names = list(frames)
    print(f"  有效因子 {len(names)} 个，截面 {len(sample)} 个")
    acc = pd.DataFrame(0.0, index=names, columns=names)
    count = pd.DataFrame(0.0, index=names, columns=names)
    for dt in sample:
        cross = pd.DataFrame({name: frames[name].loc[dt] for name in names})
        cross = cross.dropna(how="all")
        if len(cross) < 30:
            continue
        corr = cross.rank().corr(method="pearson", min_periods=30)
        valid = corr.notna()
        acc = acc.add(corr.fillna(0.0), fill_value=0.0)
        count = count.add(valid.astype(float), fill_value=0.0)
    return (acc / count.replace(0, np.nan)).round(4)


def family_matrix(corr: pd.DataFrame) -> pd.DataFrame:
    fam = pd.Series({f: diag.family_of(f) for f in corr.index})
    out = pd.DataFrame(index=sorted(fam.unique()), columns=sorted(fam.unique()), dtype=float)
    for a in out.index:
        for b in out.columns:
            block = corr.loc[fam[fam == a].index, fam[fam == b].index]
            out.loc[a, b] = float(np.nanmean(block.to_numpy()))
    return out.round(3)


def clusters(corr: pd.DataFrame, threshold: float) -> list[list[str]]:
    names = list(corr.index)
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            value = corr.loc[a, b]
            if np.isfinite(value) and abs(value) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def heatmap_html(fam: pd.DataFrame, title: str) -> str:
    labels = list(fam.index)
    cells = []
    for a in labels:
        row = [f'<td class="lab">{a} · {diag.family_label(a)}</td>']
        for b in labels:
            value = fam.loc[a, b]
            if not np.isfinite(value):
                color = "#fff"
            elif value >= 0:
                alpha = min(0.92, abs(value))
                color = f"rgba(31,58,95,{alpha:.2f})"
            else:
                alpha = min(0.92, abs(value))
                color = f"rgba(140,47,57,{alpha:.2f})"
            text_color = "#fff" if np.isfinite(value) and abs(value) > 0.45 else "#1a1f28"
            row.append(f'<td style="background:{color};color:{text_color}">{value:+.2f}</td>')
        cells.append("<tr>" + "".join(row) + "</tr>")
    header = "".join(f"<th>{b}</th>" for b in labels)
    return (
        f"<h2>{title}</h2><div style='overflow:auto'>"
        f"<table><thead><tr><th>家族</th>{header}</tr></thead>"
        f"<tbody>{''.join(cells)}</tbody></table></div>"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="price", choices=("price", "financial"))
    parser.add_argument("--pool", type=int, default=200)
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--sample-dates", type=int, default=24)
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument("--limit", type=int, default=0, help="只算前 N 个因子（调试）")
    parser.add_argument("--out", default="output/factor_reports/audit")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    panel = load_panel(args.pool_date, args.pool, args.start, args.end)
    ids = _load_ids(args.source)
    if args.limit:
        ids = ids[: args.limit]
    print(f"计算 {len(ids)} 个因子的相关矩阵（抽 {args.sample_dates} 个截面）…")
    corr = compute_corr(panel, ids, args.source, args.sample_dates)
    if corr.empty:
        print("没有可计算的因子")
        return 1
    corr.to_csv(out / "factor_corr.csv", encoding="utf-8-sig")
    fam = family_matrix(corr)
    fam.to_csv(out / "family_corr.csv", encoding="utf-8-sig")
    pairs = []
    names = list(corr.index)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            value = corr.loc[a, b]
            if np.isfinite(value) and abs(value) >= args.threshold:
                pairs.append({"a": a, "b": b, "rho": round(float(value), 4)})
    pd.DataFrame(pairs).to_csv(out / "high_corr_pairs.csv", index=False, encoding="utf-8-sig")
    groups = clusters(corr, args.threshold)
    (out / "clusters.json").write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = "".join(
        f"<tr><td>{i+1}</td><td>{'、'.join(g)}</td><td>{len(g)}</td></tr>" for i, g in enumerate(groups)
    )
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>因子相关矩阵</title><style>{CSS}</style></head><body><div class="page">
<h1>因子相关矩阵与家族聚类</h1>
<div class="sub">{args.source} · 池 {args.pool} · {args.start} 起 · 抽样 {args.sample_dates} 个截面 ·
阈值 |ρ|≥{args.threshold:.2f}</div>
<p class="small">逐日截面 Spearman 后对日期取均值。色深=相关强度，藏青=正相关，酒红=负相关。
同一家族内 20d/21d/42d 等窗口高度共线，评级只应保留一个代表。</p>
{heatmap_html(fam, "家族平均相关矩阵（20×20）")}
<h2>高相关簇（|ρ|≥{args.threshold:.2f}）</h2>
<table><thead><tr><th>#</th><th>成员</th><th>数量</th></tr></thead><tbody>{rows}</tbody></table>
<p class="small">建议：每个簇只保留一个代表（优先 IC 高、样本长、换手低者），
其余作为变体附表，不再独立打分。全矩阵见 factor_corr.csv。</p>
</div></body></html>"""
    (out / "corr_report.html").write_text(html, encoding="utf-8")
    print(f"相关矩阵 {corr.shape}，家族 {fam.shape}，高相关簇 {len(groups)} 个 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
