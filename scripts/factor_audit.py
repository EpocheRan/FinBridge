#!/usr/bin/env python
"""全库因子审计：对每份报告 JSON 跑一致性断言，输出违规清单与家族汇总。

用法::

    python scripts/factor_audit.py --dir output/factor_reports
    python scripts/factor_audit.py --dir output/factor_reports --out output/factor_reports/audit

输出：``audit_<run>.csv`` / ``.json`` / ``.md`` / ``library_report.html``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
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


def load_payloads(directory: Path) -> list[dict]:
    payloads = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("audit") or path.name.startswith("factor_corr"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if "factor" in data and "diagnostics" in data:
            data["_path"] = str(path)
            payloads.append(data)
    return payloads


def _num(value, default=np.nan) -> float:
    try:
        out = float(value)
        return out if np.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _html_run_id(json_path: str) -> str | None:
    """从 HTML 页脚读出 run_id，用于核对「HTML 与 JSON 是否同源」。"""
    path = Path(json_path).with_suffix(".html")
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"RUN</span>\s*([^<\s]+)", text)
    return match.group(1) if match else None


def _primary_stats(payload: dict) -> tuple[float, float, float]:
    stats = payload.get("ic_stats") or {}
    key = "5" if "5" in stats else (next(iter(stats)) if stats else None)
    if key is None:
        return np.nan, np.nan, np.nan
    row = stats[key]
    return _num(row.get("mean")), _num(row.get("t_indep")), _num(row.get("ir"))


def build_rows(payloads: list[dict]) -> pd.DataFrame:
    rows = []
    for data in payloads:
        ic, t, icir = _primary_stats(data)
        rating = data.get("rating") or {}
        gross = data.get("spread_gross") or {}
        net = data.get("spread_net") or {}
        sample = data.get("sample") or {}
        checks = data.get("diagnostics") or []
        bad = [c for c in checks if c.get("status") in ("fail", "warn")]
        warn_ids = [c.get("check_id") for c in checks if c.get("status") == "warn"]
        status = data.get("checks_status")
        # 只有「不可交易区间跨成本」一条提示时标为 boundary，不淹没在 warn 里
        if status == "warn" and warn_ids and set(warn_ids) <= {"tradeability"}:
            status = "boundary"
        html_run = _html_run_id(data.get("_path", ""))
        rows.append(
            {
                "factor": data.get("factor"),
                "family": data.get("family") or diag.family_of(data.get("factor", "")),
                "variant": diag.variant_of(data.get("factor", "")),
                "family_label": diag.family_label(data.get("family") or diag.family_of(data.get("factor", ""))),
                "grade": rating.get("grade"),
                "raw_grade": rating.get("raw_grade"),
                "score": _num(rating.get("score")),
                "capped": bool(rating.get("capped")),
                "ic": ic,
                "ic_t": t,
                "icir": icir,
                "mono": _num(data.get("monotonicity")),
                "mono_net": _num(data.get("monotonicity_net")),
                "spread_gross": _num(gross.get("mean")),
                "spread_net": _num(net.get("mean")),
                "spread_t": _num(net.get("t")),
                "cost": _num(data.get("cost_mean")),
                "coverage": _num(sample.get("coverage")),
                "n_dates": int(_num(sample.get("n_dates"), 0)),
                "direction": data.get("direction"),
                "direction_source": data.get("direction_source"),
                "checks_status": status,
                "fail_checks": ",".join(c.get("check_id") for c in checks if c.get("status") == "fail"),
                "warn_checks": ",".join(c.get("check_id") for c in checks if c.get("status") == "warn"),
                "attribution": (data.get("attribution") or {}).get("label"),
                "run_id": data.get("run_id"),
                "html_run_id": html_run,
                "run_id_match": html_run == data.get("run_id"),
                "n_bad": len(bad),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["family", "score"], ascending=[True, False]).reset_index(drop=True)
        frame["family_pct"] = frame.groupby("family")["score"].rank(pct=True)
    return frame


def family_consistency(frame: pd.DataFrame, min_abs_ic: float = 0.02) -> list[dict]:
    """同变体检查：同一公式/窗口族的 IC 同号、价差量级不出现 3 倍以上断裂。"""
    issues = []
    for family, group in frame.groupby("variant"):
        strong = group[group["ic"].abs() >= min_abs_ic]
        if len(strong) < 2:
            continue
        signs = set(np.sign(strong["ic"].dropna()).astype(int))
        if len(signs) > 1:
            issues.append(
                {
                    "family": family,
                    "issue": "同族 IC 符号不一致",
                    "detail": "、".join(f"{r.factor}:{r.ic:+.3f}" for r in strong.itertuples()),
                }
            )
        same = strong[np.sign(strong["ic"]) == np.sign(strong["ic"].iloc[0])]
        dup = same.round({"ic": 6, "spread_gross": 8}).duplicated(["ic", "spread_gross"], keep=False)
        if dup.any():
            issues.append(
                {
                    "family": family,
                    "issue": "同族完全同值（重复因子）",
                    "detail": "、".join(
                        f"{r.factor}:IC{r.ic:+.3f}/价差{r.spread_gross*100:+.3f}%"
                        for r in same[dup].itertuples()
                    ),
                }
            )
        spreads = same["spread_gross"].abs().replace(0, np.nan).dropna()
        if len(spreads) >= 2 and spreads.max() / spreads.min() > 3:
            issues.append(
                {
                    "family": family,
                    "issue": "同族价差量级差 >3×",
                    "detail": "、".join(
                        f"{r.factor}:IC{r.ic:+.3f}/价差{r.spread_gross*100:+.2f}%"
                        for r in same.itertuples()
                    ),
                }
            )
    return issues


def summarize(frame: pd.DataFrame) -> dict:
    dist = diag.grade_distribution(frame["grade"].dropna().tolist())
    attribution = Counter(frame.loc[frame["grade"] == "D", "attribution"].dropna().tolist())
    return {
        "n": int(len(frame)),
        "grades": dist["counts"],
        "grade_shares": dist["shares"],
        "checks": {
            "pass": int((frame["checks_status"] == "pass").sum()),
            "boundary": int((frame["checks_status"] == "boundary").sum()),
            "warn": int((frame["checks_status"] == "warn").sum()),
            "fail": int((frame["checks_status"] == "fail").sum()),
        },
        "attribution": dict(attribution),
        "run_ids": sorted(frame["run_id"].dropna().unique().tolist()),
        "run_id_mismatch": int((~frame["run_id_match"].fillna(False)).sum()),
        "families": int(frame["family"].nunique()),
    }


def write_markdown(frame: pd.DataFrame, summary: dict, issues: list[dict], path: Path) -> None:
    lines = [
        "# 因子库审计",
        "",
        f"- 因子数：{summary['n']}；家族数：{summary['families']}",
        f"- 等级分布：" + " / ".join(f"{g} {summary['grades'].get(g, 0)}" for g in "ABCD"),
        f"- 断言：pass {summary['checks']['pass']} / boundary {summary['checks']['boundary']} / warn {summary['checks']['warn']} / fail {summary['checks']['fail']}",
        f"- HTML/JSON run_id 不一致：{summary['run_id_mismatch']} 份",
        f"- D 级失效归因：" + (" / ".join(f"{k} {v}" for k, v in summary["attribution"].items()) or "无"),
        f"- run_id：{', '.join(summary['run_ids']) or '—'}",
        "",
        "## 违规清单",
        "",
    ]
    bad = frame[(frame["checks_status"] != "pass") | (~frame["run_id_match"].fillna(False))]
    if bad.empty:
        lines.append("无。")
    else:
        for row in bad.itertuples():
            extra = "" if row.run_id_match else f"；run_id 不一致（JSON {row.run_id} / HTML {row.html_run_id}）"
            lines.append(
                f"- **{row.factor}**（{row.grade}）：{row.checks_status}；"
                f"fail={row.fail_checks or '—'}；warn={row.warn_checks or '—'}；"
                f"IC {row.ic:+.3f}，ρ {row.mono:+.2f}，毛价差 {row.spread_gross*100:+.2f}%/期{extra}"
            )
    lines += ["", "## 同族一致性", ""]
    if not issues:
        lines.append("无。")
    else:
        for issue in issues:
            lines.append(f"- **{issue['family']}**：{issue['issue']}；{issue['detail']}")
    lines += ["", "## 家族汇总", ""]
    lines.append("| 家族 | 数量 | A/B/C/D | 均分 | 断言失败 |")
    lines.append("|---|---|---|---|---|")
    for family, group in frame.groupby("family"):
        counts = Counter(group["grade"].dropna())
        lines.append(
            f"| {family} | {len(group)} | {counts.get('A',0)}/{counts.get('B',0)}/"
            f"{counts.get('C',0)}/{counts.get('D',0)} | {group['score'].mean():.1f} | "
            f"{int((group['checks_status']=='fail').sum())} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


CSS = """
:root{--ink:#1a1f28;--muted:#6b7280;--rule:#d9dee4;--navy:#1f3a5f;--burgundy:#8c2f39;--teal:#2c6e6a;--gold:#a8842c;--wash:#f6f7f9}
*{box-sizing:border-box}body{margin:0;background:#eceef1;color:var(--ink);font-family:"Microsoft YaHei",-apple-system,"Segoe UI",sans-serif;font-size:13px;line-height:1.6}
.page{max-width:1180px;margin:28px auto;background:#fff;padding:40px 48px;box-shadow:0 2px 24px rgba(20,30,45,.08)}
h1{font-family:Georgia,"SimSun",serif;font-size:30px;margin:0 0 4px}h2{font-family:Georgia,"SimSun",serif;font-size:20px;margin:30px 0 10px;border-bottom:1px solid var(--rule);padding-bottom:6px}
.sub{color:var(--muted);margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
.kpi{border:1px solid var(--rule);padding:12px 14px;background:var(--wash)}.kpi b{display:block;font-size:24px;font-family:Georgia,serif}
table{border-collapse:collapse;width:100%;margin:8px 0 16px}th,td{padding:5px 7px;border-bottom:1px solid var(--rule);text-align:right}
th:first-child,td:first-child{text-align:left}thead th{font-size:11px;color:var(--muted);border-bottom:1.5px solid var(--ink)}
.fail{color:var(--burgundy);font-weight:600}.warn{color:var(--gold);font-weight:600}.pass{color:var(--teal)}.boundary{color:var(--gold)}
.small{font-size:11.5px;color:var(--muted)}.bar{display:inline-block;height:8px;background:var(--navy);vertical-align:middle}
"""


def write_family_reports(frame: pd.DataFrame, out_dir: Path) -> int:
    """每个家族一份摘要页；单因子 HTML 作为附录，不再 143 份同构报告平铺。"""
    fam_dir = out_dir / "families"
    fam_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for family, group in frame.groupby("family"):
        counts = Counter(group["grade"].dropna())
        rows = []
        for row in group.sort_values("score", ascending=False).itertuples():
            rows.append(
                f"<tr><td>{row.factor}</td><td>{row.grade}</td><td>{row.score:.1f}</td>"
                f"<td>{row.family_pct:.0%}</td>"
                f"<td>{row.ic:+.3f}</td><td>{row.ic_t:+.1f}</td><td>{row.mono:+.2f}</td>"
                f"<td>{row.spread_gross*100:+.2f}%</td><td>{row.spread_net*100:+.2f}%</td>"
                f"<td>{row.attribution or '—'}</td></tr>"
            )
        body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>家族报告 · {family}</title><style>{CSS}</style></head><body><div class="page">
<h1>{family} · {diag.family_label(family)}</h1>
<div class="sub">家族成员 {len(group)} 个 · A/B/C/D = {counts.get('A',0)}/{counts.get('B',0)}/{counts.get('C',0)}/{counts.get('D',0)}
 · 均分 {group['score'].mean():.1f}</div>
<div class="kpis">
<div class="kpi"><b>{group['ic'].mean():+.3f}</b>平均 IC</div>
<div class="kpi"><b>{group['spread_gross'].mean()*100:+.2f}%</b>平均毛价差/期</div>
<div class="kpi"><b>{group['mono'].mean():+.2f}</b>平均 ρ</div>
<div class="kpi"><b>{int((group['checks_status']=='fail').sum())}</b>断言失败</div>
</div>
<p class="small">家族内同源（同公式、不同窗口）的因子高度共线，本页只用于族内排序与去重；
<b>禁止跨家族比较等级</b>。成员窗口差异见因子 ID；相关矩阵见 library_report。</p>
<table><thead><tr><th>因子</th><th>等级</th><th>分数</th><th>族内分位</th><th>IC</th><th>独立 t</th><th>ρ</th>
<th>毛价差</th><th>净价差</th><th>失效归因</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</div></body></html>"""
        (fam_dir / f"{family.replace('.', '_')}.html").write_text(
            re.sub(r"\+?nan\b", "—", body), encoding="utf-8"
        )
        count += 1
    return count


def write_html(frame: pd.DataFrame, summary: dict, issues: list[dict], path: Path) -> None:
    grade_counts = summary["grades"]
    kpis = "".join(
        [
            f'<div class="kpi"><b>{summary["n"]}</b>因子数</div>',
            f'<div class="kpi"><b>{grade_counts.get("A",0)}/{grade_counts.get("B",0)}/{grade_counts.get("C",0)}/{grade_counts.get("D",0)}</b>A/B/C/D</div>',
            f'<div class="kpi"><b>{summary["checks"]["fail"]}</b>断言失败</div>',
            f'<div class="kpi"><b>{summary["families"]}</b>家族数</div>',
        ]
    )
    fam_rows = []
    for family, group in frame.groupby("family"):
        counts = Counter(group["grade"].dropna())
        link = f"families/{family.replace('.', '_')}.html"
        fam_rows.append(
            f"<tr><td><a href='{link}'>{family} · {diag.family_label(family)}</a></td><td>{len(group)}</td>"
            f"<td>{counts.get('A',0)}/{counts.get('B',0)}/{counts.get('C',0)}/{counts.get('D',0)}</td>"
            f"<td>{group['score'].mean():.1f}</td><td>{int((group['checks_status']=='fail').sum())}</td>"
            f"<td>{group['ic'].mean():+.3f}</td><td>{group['spread_gross'].mean()*100:+.2f}%</td></tr>"
        )
    issue_rows = "".join(
        f"<tr><td>{i['family']}</td><td class='fail'>{i['issue']}</td><td>{i['detail']}</td></tr>"
        for i in issues
    ) or "<tr><td colspan='3'>无</td></tr>"
    detail_rows = []
    for row in frame.itertuples():
        cls = "pass" if row.checks_status == "pass" else ("fail" if row.checks_status == "fail" else "warn")
        detail_rows.append(
            f"<tr><td>{row.factor}</td><td>{row.family}</td><td>{row.grade}</td><td>{row.score:.1f}</td>"
            f"<td>{row.family_pct:.0%}</td><td>{row.ic:+.3f}</td><td>{row.ic_t:+.1f}</td><td>{row.mono:+.2f}</td>"
            f"<td>{row.spread_gross*100:+.2f}%</td><td>{row.spread_net*100:+.2f}%</td>"
            f"<td class='{cls}'>{row.checks_status}</td><td>{row.attribution or '—'}</td></tr>"
        )
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>因子库审计</title><style>{CSS}</style></head><body><div class="page">
<h1>因子库审计与家族汇总</h1>
<div class="sub">run_id：{', '.join(summary['run_ids']) or '—'} · 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<p class="small">附表：<a href="corr_report.html">相关矩阵与家族聚类</a> ·
<a href="high_corr_pairs.csv">高相关对（|ρ|≥0.90）</a> ·
<a href="factor_corr.csv">全量相关矩阵</a></p>
<div class="kpis">{kpis}</div>
<p class="small">本页只汇总可复核字段；单因子结论见各自 HTML。等级为<b>家族内局部评级</b>，
价量族与财务族的样本池/窗口不同，禁止跨族比较。等级分布目标 A 5–10% / B 15–20%，
实际 A {summary['grade_shares'].get('A',0):.1%} / B {summary['grade_shares'].get('B',0):.1%}；
若偏离过大，应先修断言再校准分数线，而不是给坏尺子调刻度。</p>
<h2>家族汇总</h2>
<table><thead><tr><th>家族</th><th>数量</th><th>A/B/C/D</th><th>均分</th><th>断言失败</th><th>平均 IC</th><th>平均毛价差</th></tr></thead>
<tbody>{''.join(fam_rows)}</tbody></table>
<h2>同族一致性</h2>
<table><thead><tr><th>家族</th><th>问题</th><th>明细</th></tr></thead><tbody>{issue_rows}</tbody></table>
<h2>全部因子</h2>
<table><thead><tr><th>因子</th><th>家族</th><th>等级</th><th>分数</th><th>族内分位</th><th>IC</th><th>独立 t</th><th>ρ</th>
<th>毛价差/期</th><th>净价差/期</th><th>断言</th><th>失效归因</th></tr></thead>
<tbody>{''.join(detail_rows)}</tbody></table>
<p class="small">断言定义：ρ 与 Q5−Q1 恒等式（|ρ|≥0.8 硬边界）、IC→价差传导（实测/期望 0~2）、
价差 vs 成本区间、方向与先验一致。warn 不一定是错误，fail 必须回查。</p>
</div></body></html>"""
    path.write_text(re.sub(r"\+?nan\b", "—", html), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="全库因子审计")
    parser.add_argument("--dir", default="output/factor_reports")
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-abs-ic", type=float, default=0.02)
    args = parser.parse_args()

    directory = Path(args.dir)
    payloads = load_payloads(directory)
    if not payloads:
        print(f"{directory} 下没有可审计的 JSON；先跑 scripts/factor_scorecard.py")
        return 1
    frame = build_rows(payloads)
    summary = summarize(frame)
    issues = family_consistency(frame, args.min_abs_ic)

    out = Path(args.out) if args.out else directory / "audit"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M")
    frame.to_csv(out / f"audit_{stamp}.csv", index=False, encoding="utf-8-sig")
    (out / f"audit_{stamp}.json").write_text(
        json.dumps({"summary": summary, "issues": issues}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(frame, summary, issues, out / f"audit_{stamp}.md")
    write_html(frame, summary, issues, out / "library_report.html")
    n_families = write_family_reports(frame, out)

    print(f"审计 {summary['n']} 个因子 / {summary['families']} 个家族")
    print("等级分布", summary["grades"], "断言", summary["checks"])
    print("D 级归因", summary["attribution"])
    print("同族问题", len(issues), "→", out, f"（{n_families} 份家族报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
