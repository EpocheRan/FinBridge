"""把 :class:`FactorReport` 渲染成教科书式 HTML（内联 SVG，可打印）。"""

from __future__ import annotations

import html
from datetime import datetime

import numpy as np
import pandas as pd

from . import charts

CSS = """
:root{
  --ink:#1a1f28; --muted:#6b7280; --faint:#9aa3ae; --rule:#d9dee4;
  --navy:#1f3a5f; --burgundy:#8c2f39; --teal:#2c6e6a; --gold:#a8842c;
  --paper:#ffffff; --wash:#f6f7f9;
}
*{box-sizing:border-box}
body{margin:0;background:#eceef1;color:var(--ink);
  font-family:"Source Han Sans SC","Microsoft YaHei",-apple-system,"Segoe UI",sans-serif;
  font-size:14px;line-height:1.72;-webkit-font-smoothing:antialiased;}
.page{max-width:1120px;margin:32px auto;background:var(--paper);
  padding:64px 72px 72px;box-shadow:0 2px 24px rgba(20,30,45,.08);}
h1,h2,h3,.serif{font-family:Georgia,"Source Han Serif SC","SimSun",serif;font-weight:600;}
.eyebrow{font-size:11px;letter-spacing:.22em;color:var(--faint);text-transform:uppercase;}
.masthead{display:flex;justify-content:space-between;align-items:flex-end;
  border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:28px;}
.masthead h1{font-size:34px;margin:6px 0 6px;letter-spacing:-.01em;}
.masthead .sub{color:var(--muted);font-size:13px;}
.badge{text-align:center;min-width:120px;}
.badge .grade{font-family:Georgia,serif;font-size:56px;line-height:1;color:var(--burgundy);}
.badge .score{font-size:12px;color:var(--muted);margin-top:6px;letter-spacing:.08em;}
.abstract{border-left:3px solid var(--navy);background:var(--wash);
  padding:16px 20px;margin:0 0 30px;font-size:14.5px;}
.abstract b{color:var(--navy);}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:0 0 34px;}
.kpi{border:1px solid var(--rule);padding:14px 16px;}
.kpi .label{font-size:11px;color:var(--muted);letter-spacing:.08em;}
.kpi .value{font-family:Georgia,serif;font-size:30px;margin-top:4px;}
.kpi .sub{font-size:11.5px;color:var(--faint);margin-top:2px;}
h2{font-size:20px;margin:44px 0 6px;padding-bottom:8px;border-bottom:1px solid var(--rule);}
h2 .no{color:var(--faint);font-size:13px;margin-right:10px;letter-spacing:.1em;}
h3{font-size:15px;margin:26px 0 8px;color:var(--navy);}
p{margin:8px 0;}
.figure{margin:18px 0 26px;}
.figure .cap{font-size:12px;color:var(--muted);margin-top:8px;}
.figure .cap b{color:var(--ink);font-weight:600;margin-right:8px;}
.figure .note{font-size:11.5px;color:var(--faint);margin-top:2px;}
table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0 6px;
  font-variant-numeric:tabular-nums;}
th,td{padding:7px 8px;border-bottom:1px solid var(--rule);text-align:right;}
th:first-child,td:first-child{text-align:left;}
thead th{font-weight:600;color:var(--muted);font-size:11.5px;letter-spacing:.06em;
  border-bottom:1.5px solid var(--ink);}
tbody tr:last-child td{border-bottom:1.5px solid var(--ink);}
.pos{color:var(--burgundy);} .neg{color:var(--teal);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px;align-items:start;}
.grid3{display:grid;grid-template-columns:1.1fr .9fr;gap:32px;align-items:center;}
.verdict{border:1px solid var(--rule);padding:18px 20px;background:var(--wash);}
.verdict h3{margin-top:0;}
.verdict ul{margin:6px 0 0 18px;padding:0;}
.verdict li{margin:4px 0;}
.warn{border-left:3px solid var(--gold);background:#fdf8ec;padding:10px 14px;
  font-size:12.5px;color:#6b5a20;margin:12px 0;}
.footer{margin-top:46px;padding-top:14px;border-top:1px solid var(--rule);
  font-size:11px;color:var(--faint);display:flex;justify-content:space-between;}
.tag{display:inline-block;font-size:10.5px;letter-spacing:.08em;color:var(--muted);
  border:1px solid var(--rule);padding:1px 7px;margin-right:6px;}
@media print{
  body{background:#fff;font-size:11.5px}
  .page{box-shadow:none;margin:0;padding:0 0 12mm;max-width:none}
  h2{page-break-after:avoid}
  .figure,table{page-break-inside:avoid}
}
"""


def _esc(value) -> str:
    return html.escape(str(value))


def _cls(value: float) -> str:
    if value != value:
        return ""
    return "pos" if value > 0 else ("neg" if value < 0 else "")


def _pct(value: float, digits: int = 2) -> str:
    if value != value:
        return "—"
    return f"{value * 100:+.{digits}f}%"


def _num(value: float, digits: int = 3) -> str:
    if value != value:
        return "—"
    return f"{value:.{digits}f}"


def _time_points(series: pd.Series):
    series = series.dropna()
    points = [(float(i), float(v)) for i, v in enumerate(series.to_numpy())]
    n = len(series)
    ticks = 6 if n > 24 else max(2, n // 12)
    labels, step = [], max(1, n // max(1, ticks - 1))
    for i in range(0, n, step):
        labels.append(pd.Timestamp(series.index[i]).strftime("%Y-%m"))
    return points, labels


def _figure(no: str, title: str, svg: str, note: str = "") -> str:
    note_html = f'<div class="note">{note}</div>' if note else ""
    return (
        f'<div class="figure">{svg}'
        f'<div class="cap"><b>图 {no}</b>{_esc(title)}</div>{note_html}</div>'
    )


def _table(headers, rows, note: str = "") -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{cell[1]}">{cell[0]}</td>' if isinstance(cell, tuple)
                         else f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    note_html = f'<div class="note">{note}</div>' if note else ""
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{note_html}"


def _kpi(label: str, value: str, sub: str) -> str:
    return (f'<div class="kpi"><div class="label">{_esc(label)}</div>'
            f'<div class="value">{_esc(value)}</div><div class="sub">{_esc(sub)}</div></div>')


def render(report, rating: dict, meta: dict) -> str:
    stats = report.ic_stats
    primary = 5 if 5 in stats.index else (stats.index[0] if len(stats) else 5)
    row = stats.loc[primary] if primary in stats.index else None

    ic_mean = float(row["mean"]) if row is not None else float("nan")
    ic_ir = float(row["ir"]) if row is not None else float("nan")
    ic_t = float(row["t"]) if row is not None else float("nan")
    ic_win = float(row["win"]) if row is not None else float("nan")
    ls = report.long_short

    # ---------------- 摘要
    summary = (
        f"<b>{_esc(report.factor_id)}</b> 在 {_esc(report.universe)} 样本上，"
        f"{primary} 日持有期 IC 均值 <b>{_num(ic_mean)}</b>、ICIR <b>{_num(ic_ir, 2)}</b>"
        f"（Newey-West t = {_num(ic_t, 1)}，IC 胜率 {ic_win:.0%}）；"
        f"分层单调性 {_num(rating['meta']['monotonicity'], 2)}，"
        f"多空组合年化 {_pct(ls.get('ann', float('nan')), 1)}、夏普 {_num(ls.get('sharpe', float('nan')), 2)}。"
        f"时间维度得分 {rating['time_score']:.0f}/100、空间维度 {rating['space_score']:.0f}/100，"
        f"定位于「<b>{rating['quadrant']}</b>」。"
    )

    kpis = "".join(
        [
            _kpi("Rank IC（h=5）", _num(ic_mean, 3), f"IC 胜率 {ic_win:.0%}"),
            _kpi("ICIR", _num(ic_ir, 2), f"NW t = {_num(ic_t, 1)}"),
            _kpi("多空夏普", _num(ls.get("sharpe", float("nan")), 2),
                 f"年化 {_pct(ls.get('ann', float('nan')), 1)}"),
            _kpi("覆盖率", f"{report.coverage:.1%}", f"{report.n_symbols} 只 · {report.n_dates} 日"),
        ]
    )

    # ---------------- 图：滚动 IC
    rolling_fig = ""
    if report.rolling_ic is not None and report.rolling_ic.notna().sum() > 10:
        points, labels = _time_points(report.rolling_ic)
        raw = report.ic.get(primary)
        raw_points = []
        if raw is not None:
            clean = raw.dropna()
            if len(clean) > 20:
                limit = max(3 * float(clean.std()), 0.10)
                clean = clean.clip(-limit, limit)
            raw_points, _ = _time_points(clean)
        svg = charts.line_chart(
            [
                charts.Series("滚动均值", points, color=charts.NAVY, width=2.2),
            ],
            frame=charts.ChartFrame(width=980, height=280),
            band=(-0.05, 0.05),
            x_labels=labels,
            y_formatter=lambda v: f"{v:+.2f}",
            caption="",
        )
        rolling_fig = _figure(
            "2-1", f"IC 时序与滚动均值（h={primary}）", svg,
            "注：藏青线为滚动均值；阴影为 ±0.05 的弱信号带，线体长期贴近 0 轴说明预测力薄弱。"
            "T 日收盘出信号，T+1 开盘买入。",
        )

    # ---------------- 图：分年 IC
    yearly_fig = ""
    if report.ic_by_year is not None and len(report.ic_by_year) > 1:
        svg = charts.bar_chart(
            [str(y) for y in report.ic_by_year.index],
            list(report.ic_by_year.to_numpy()),
            width=460,
            height=240,
            formatter=lambda v: f"{v:+.3f}",
            caption="",
        )
        yearly_fig = _figure("2-2", "分年度 IC（检验跨周期稳定性）", svg,
                             "注：柱体为当年 IC 均值；同号率低于 60% 时评级强制降档。")

    # ---------------- 图：IC 衰减
    decay_fig = ""
    if report.decay is not None and len(report.decay) > 1:
        points = [(float(h), float(v)) for h, v in report.decay.items()]
        svg = charts.line_chart(
            [charts.Series("IC", points, color=charts.BURGUNDY, width=2.4)],
            frame=charts.ChartFrame(width=460, height=230, bottom=44),
            y_formatter=lambda v: f"{v:+.3f}",
            x_labels=[f"h={int(h)}" for h in report.decay.index],
            caption="",
        )
        decay_fig = _figure("2-3", "IC 衰减曲线", svg,
                            "注：横轴为持有期（交易日）。半衰期短于目标持有期时，需提高换仓频率或降低权重。")

    # ---------------- 图：分组平均收益（比复利净值更干净）
    nav_fig = ""
    if report.quantile_returns:
        labels = list(report.quantile_returns)
        values = [report.quantile_returns[k] * 100 for k in labels]
        svg = charts.bar_chart(labels, values, width=980, height=250,
                               formatter=lambda v: f"{v:+.2f}%",
                               caption="")
        nav_fig = _figure("3-1", f"五分组平均持有期收益（h={primary}，T+1 开盘买入，扣 20bp）", svg,
                          "注：用「每期收益的均值」而非复利终值衡量分层优劣——后者会被波动拖累污染。"
                          "柱高应随分组单调上升（或单调下降）才算合格。")

    # ---------------- 表：分层收益
    qrows = []
    for col, value in report.quantile_returns.items():
        qrows.append([col, (f"{value * 100:+.2f}%", _cls(value))])
    qrows.append(["单调性 ρ", (f"{report.monotonicity:.2f}", "")])
    qrows.append(["Top 组换手", (f"{report.turnover:.0%}", "")])
    quant_table = _table(["分组", "平均每期收益"], qrows,
                         "注：非重叠换仓，每期 5 个交易日；已扣除双边 20bp；与图 3-1 同源。")

    # ---------------- 图：行业 IC
    industry_fig = ""
    if report.ic_by_industry is not None and len(report.ic_by_industry) >= 3:
        top = report.ic_by_industry
        if len(top) > 12:
            top = pd.concat([top.head(6), top.tail(6)])
        svg = charts.bar_chart([str(k)[:6] for k in top.index], list(top.to_numpy()),
                               height=250, formatter=lambda v: f"{v:+.3f}", caption="")
        industry_fig = _figure("3-2", "分行业 IC（中性化前）", svg,
                               "注：仅展示 IC 最高/最低各 6 个行业；行业间离散度过大说明因子带行业暴露。")

    # ---------------- 表：可成交性
    tradeable_rows = []
    if report.limit_up:
        lu = report.limit_up
        delta = (lu["ic_masked"] - lu["ic_raw"])
        tradeable_rows = [
            ["不可成交样本占比", f"{lu['excluded_ratio']:.2%}"],
            ["IC（原始）", f"{lu['ic_raw']:+.3f}"],
            ["IC（剔除不可成交）", f"{lu['ic_masked']:+.3f}"],
            ["影响", (f"{delta:+.3f}", _cls(delta))],
            ["结论", "影响可忽略" if abs(delta) < 0.003 else "存在实质性高估，应使用剔除后口径"],
        ]

    # ---------------- 表：分市值 / 分状态
    size_rows = []
    if report.ic_by_size is not None:
        for k, v in report.ic_by_size.items():
            size_rows.append([k, (f"{v:+.3f}", _cls(v))])
    regime_rows = []
    if report.ic_by_regime is not None:
        for k, v in report.ic_by_regime.items():
            label = {"bull": "牛市", "neutral": "震荡", "bear": "熊市"}.get(str(k), str(k))
            regime_rows.append([label, (f"{v:+.3f}", _cls(v))])

    # ---------------- 评级卡
    score_svg = charts.score_bars(rating["items"], width=980, caption="")
    verdict_items = "".join(f"<li>{_esc(v)}</li>" for v in rating["vetoes"]) or "<li>无</li>"
    quad_svg = charts.quadrant(
        rating["time_score"], rating["space_score"],
        labels=[(90, 70, "择时型"), (330, 70, "核心型"), (90, 330, "淘汰"), (330, 330, "过滤型")],
    )

    warn_html = ""
    if report.warnings:
        warn_html = '<div class="warn"><b>诊断提示</b><br>' + "<br>".join(
            _esc(w) for w in report.warnings) + "</div>"

    # ---------------- 附录
    detail_rows = []
    for h in stats.index:
        r = stats.loc[h]
        detail_rows.append([f"{int(h)} 日", (f"{r['mean']:+.3f}", _cls(r["mean"])),
                            f"{r['ir']:.2f}", f"{r['t']:.1f}", f"{r['win']:.0%}", f"{int(r['n'])}"])
    detail_table = _table(["持有期", "IC 均值", "ICIR", "NW t", "IC 胜率", "样本日"], detail_rows,
                          "注：Newey-West 滞后阶数取 h-1；ICIR 年化系数 √(242/h)。")

    meta_rows = [
        ["因子 ID", report.factor_id],
        ["类别", report.category],
        ["方向", "正向（值越大越好）" if report.direction >= 0 else "反向（值越小越好）"],
        ["样本池", report.universe],
        ["样本区间", f"{report.start.date()} ~ {report.end.date()}"],
        ["标的数 / 交易日", f"{report.n_symbols} / {report.n_dates}"],
        ["覆盖率", f"{report.coverage:.1%}"],
        ["成交口径", "T 日收盘出信号，T+1 开盘买入，T+h 收盘卖出"],
        ["成本假设", "双边 20bp（佣金+印花税+滑点）"],
        ["生成时间", meta.get("generated_at", "")],
        ["run_id", meta.get("run_id", "")],
    ]
    meta_table = _table(["项目", "取值"], meta_rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>因子研究报告 · {_esc(report.factor_id)}</title>
<style>{CSS}</style></head>
<body><div class="page">

<div class="masthead">
  <div>
    <div class="eyebrow">Factor Research Note · 因子研究报告</div>
    <h1>{_esc(report.factor_id)}</h1>
    <div class="sub">{_esc(report.category)} · {_esc(report.universe)} ·
      {report.start.strftime('%Y-%m')} – {report.end.strftime('%Y-%m')} ·
      IC 口径 h={primary} · T+1 开盘成交</div>
  </div>
  <div class="badge">
    <div class="grade">{_esc(rating['grade'])}</div>
    <div class="score">{rating['score']:.0f} / 100</div>
  </div>
</div>

<div class="abstract">{summary}</div>
<div class="kpis">{kpis}</div>

<h2><span class="no">01</span>因子概览与样本</h2>
<p>本报告在统一口径下评估该因子的<b>预测力</b>（时间维度）与<b>区分度</b>（空间维度），
并对可交易性做独立检验。所有收益均按「T 日收盘出信号、T+1 开盘买入」的可执行价格计算，
不使用收盘价成交假设。</p>
{meta_table}

<h2><span class="no">02</span>时间维度：因子何时有效</h2>
<p>时间维度回答三个问题：<b>信号是否持续存在</b>（IC 均值与 t 值）、
<b>是否跨周期稳定</b>（分年、分市场状态）、<b>衰减速度多快</b>（持有期曲线）。</p>
{rolling_fig}
<div class="grid2">{yearly_fig}{decay_fig}</div>
<h3>分市场状态 IC</h3>
{_table(["市场状态", f"IC（h={primary}）"], regime_rows or [["—", "—"]],
        "注：牛/熊/震荡由中证 1000 的 20 日动量与 MA20 位置划分。三种状态同号率是评级的重要输入。")}
{industry_fig}

<h2><span class="no">03</span>空间维度：因子在谁身上有效</h2>
<p>空间维度检验横截面区分能力：分层是否单调、多空是否有经济意义、
以及在市值/行业子样本中是否同样成立。</p>
{nav_fig}
{quant_table}
<h3>可成交性检验（T+1 开盘一字涨停是否买得进）</h3>
{_table(["指标", "数值"], tradeable_rows,
        "注：涨停价按板块规则（主板 10%、创业板/科创板 20%、北交所 30%）由 t 日收盘推出；"
        "t+1 开盘价 ≥ 涨停价视为买不进，停牌同理。")}
<h3>分市值组 IC</h3>
{_table(["市值分组", f"IC（h={primary}）"], size_rows or [["—", "—"]],
        "注：按当日流通市值截面三分位划分。三组同号说明不是单一市值域的现象。")}

<h2><span class="no">04</span>评级与定位</h2>
<div class="grid3">
  <div>{quad_svg}</div>
  <div class="verdict">
    <h3>结论：{_esc(rating['grade'])} 级 · {_esc(rating['quadrant'])}</h3>
    <p>{_esc(rating['usage'])}</p>
    <p><b>一票否决项：</b></p>
    <ul>{verdict_items}</ul>
    <p><b>说明：</b>{_esc('；'.join(rating['notes']))}</p>
  </div>
</div>
{warn_html}
{score_svg}
<div class="cap" style="font-size:12px;color:var(--muted);margin-top:6px;">
  <b>图 4-1</b>评分卡（满分 100）：预测力 30 + 时间稳定性 25 + 空间单调性 15 + 可交易性 15 + 增量性 15。
</div>

<h2><span class="no">05</span>附录：指标明细与方法</h2>
{detail_table}
<p style="font-size:12px;color:var(--muted)">
<b>方法说明</b>：IC 为逐日横截面 Spearman 秩相关；t 值经 Newey-West 调整（lag = h−1），
避免重叠样本高估显著性；分层组合等权、非重叠换仓、T+1 开盘买入并扣双边 20bp；
市场状态由中证 1000 的 20 日动量与 MA20 联合判定；所有统计均逐日计算，不做全样本池化。
</p>

<div class="footer">
  <div><span class="tag">RUN</span>{_esc(meta.get('run_id', ''))}
       <span class="tag">DATA</span>{_esc(meta.get('data_version', ''))}</div>
  <div>本报告为因子研究用途，不构成投资建议。</div>
</div>

</div></body></html>"""
