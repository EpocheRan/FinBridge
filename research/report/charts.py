"""纯 SVG 图表原语（无第三方绘图依赖）。

设计原则
--------
- 输出**矢量** SVG，打印/缩放不失真；
- 配色克制：墨色文字 + 藏青/酒红/青灰三色数据线，符合出版物观感；
- 每个图自带坐标轴、零线、标注，调用方只管传数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

# ---------------------------------------------------------------- 调色板
INK = "#1a1f28"
MUTED = "#6b7280"
FAINT = "#9aa3ae"
RULE = "#d9dee4"
GRID = "#eef1f4"
NAVY = "#1f3a5f"
BURGUNDY = "#8c2f39"
TEAL = "#2c6e6a"
GOLD = "#a8842c"
SLATE = "#4a5568"
WHITE = "#ffffff"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


@dataclass
class Series:
    name: str
    points: list[tuple[float, float]]
    color: str = NAVY
    width: float = 2.0
    dashed: bool = False
    opacity: float = 1.0


@dataclass
class ChartFrame:
    """统一的绘图区域与留白。"""

    width: float = 980
    height: float = 280
    left: float = 56
    right: float = 24
    top: float = 28
    bottom: float = 40
    extras: dict = field(default_factory=dict)

    @property
    def plot_w(self) -> float:
        return self.width - self.left - self.right

    @property
    def plot_h(self) -> float:
        return self.height - self.top - self.bottom


def _scale(values: Sequence[float], lo: float, hi: float, out_lo: float, out_hi: float):
    span = hi - lo or 1.0
    return [out_lo + (v - lo) / span * (out_hi - out_lo) for v in values]


def line_chart(
    series: Sequence[Series],
    *,
    frame: ChartFrame | None = None,
    y_ticks: int = 5,
    zero_line: bool = True,
    band: tuple[float, float] | None = None,
    y_formatter=lambda v: f"{v:.2f}",
    x_labels: Sequence[str] | None = None,
    annotate_last: bool = True,
    legend: bool = False,
    caption: str | None = None,
) -> str:
    f = frame or ChartFrame()
    all_points = [p for s in series for p in s.points if p[1] == p[1]]
    if not all_points:
        return f'<svg width="{f.width}" height="{f.height}"></svg>'
    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)
    if band:
        y_lo = min(y_lo, band[0])
        y_hi = max(y_hi, band[1])
    pad = (y_hi - y_lo) * 0.12 or 0.01
    y_lo, y_hi = y_lo - pad, y_hi + pad

    def px(x: float) -> float:
        return f.left + (x - x_lo) / ((x_hi - x_lo) or 1) * f.plot_w

    def py(y: float) -> float:
        return f.top + f.plot_h - (y - y_lo) / ((y_hi - y_lo) or 1) * f.plot_h

    parts: list[str] = [f'<svg viewBox="0 0 {f.width} {f.height}" width="100%" '
                        f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect x="0" y="0" width="{f.width}" height="{f.height}" fill="{WHITE}"/>')

    # y 网格与刻度
    for i in range(y_ticks + 1):
        value = y_lo + (y_hi - y_lo) * i / y_ticks
        y = py(value)
        parts.append(f'<line x1="{f.left}" y1="{y:.1f}" x2="{f.left + f.plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{f.left - 10}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="{MUTED}">{_esc(y_formatter(value))}</text>')

    # 置信带
    if band:
        y_hi_px, y_lo_px = py(band[1]), py(band[0])
        parts.append(f'<rect x="{f.left}" y="{y_hi_px:.1f}" width="{f.plot_w}" '
                     f'height="{y_lo_px - y_hi_px:.1f}" fill="{NAVY}" opacity="0.06"/>')

    # 零线
    if zero_line and y_lo < 0 < y_hi:
        y0 = py(0)
        parts.append(f'<line x1="{f.left}" y1="{y0:.1f}" x2="{f.left + f.plot_w}" y2="{y0:.1f}" '
                     f'stroke="{SLATE}" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>')

    # 数据线
    for s in series:
        pts = [(px(x), py(y)) for x, y in s.points if y == y]
        if len(pts) < 2:
            continue
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dash = ' stroke-dasharray="5 4"' if s.dashed else ""
        parts.append(f'<path d="{d}" fill="none" stroke="{s.color}" stroke-width="{s.width}" '
                     f'stroke-linejoin="round" stroke-linecap="round" opacity="{s.opacity}"{dash}/>')
        if annotate_last and s.name:
            lx, ly = pts[-1]
            parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="{s.color}"/>')
            parts.append(f'<text x="{lx + 6:.1f}" y="{ly - 6:.1f}" font-size="10.5" '
                         f'fill="{s.color}">{_esc(s.name)}</text>')

    # x 轴
    parts.append(f'<line x1="{f.left}" y1="{f.top + f.plot_h}" x2="{f.left + f.plot_w}" '
                 f'y2="{f.top + f.plot_h}" stroke="{RULE}" stroke-width="1"/>')
    labels = list(x_labels or [])
    if labels:
        n = len(labels)
        for i, label in enumerate(labels):
            x = f.left + (f.plot_w * i / max(1, n - 1))
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            parts.append(f'<text x="{x:.1f}" y="{f.top + f.plot_h + 18:.1f}" text-anchor="{anchor}" '
                         f'font-size="11" fill="{MUTED}">{_esc(label)}</text>')

    if legend:
        lx = f.left + f.plot_w
        for s_ in reversed(list(series)):
            w = 16 + len(s_.name) * 7
            lx -= w
            parts.append(f'<rect x="{lx:.1f}" y="{f.top - 14}" width="10" height="3" '
                         f'fill="{s_.color}"/>')
            parts.append(f'<text x="{lx + 14:.1f}" y="{f.top - 9}" font-size="10.5" '
                         f'fill="{MUTED}">{_esc(s_.name)}</text>')
    if caption:
        parts.append(f'<text x="{f.left}" y="{f.height - 6}" font-size="10.5" '
                     f'fill="{FAINT}">{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    width: float = 980,
    height: float = 240,
    color_pos: str = BURGUNDY,
    color_neg: str = TEAL,
    formatter=lambda v: f"{v:.2f}",
    caption: str | None = None,
    value_labels: bool = True,
    errors: Sequence[float] | None = None,
    y_ticks: int = 5,
    y_label: str | None = None,
) -> str:
    """柱状图：带 y 轴刻度、零线、可选误差棒。"""
    left, right, top, bottom = 64, 24, 30, 46
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [0.0 if v != v else float(v) for v in values]
    errs = [0.0 if (e is None or e != e) else abs(float(e)) for e in (errors or [0.0] * len(values))]
    lows = [v - e for v, e in zip(values, errs)]
    highs = [v + e for v, e in zip(values, errs)]
    y_lo = min(min(lows), 0.0)
    y_hi = max(max(highs), 0.0)
    pad = (y_hi - y_lo) * 0.12 or (abs(y_hi) * 0.2 + 1e-6)
    y_lo, y_hi = y_lo - pad, y_hi + pad

    def py(v: float) -> float:
        return top + plot_h - (v - y_lo) / ((y_hi - y_lo) or 1) * plot_h

    step = plot_w / max(1, len(values))
    bar_w = step * 0.56

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')

    # y 网格与刻度
    for i in range(y_ticks + 1):
        value = y_lo + (y_hi - y_lo) * i / y_ticks
        y = py(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="10.5" fill="{MUTED}">{_esc(formatter(value))}</text>')

    zero = py(0)
    parts.append(f'<line x1="{left}" y1="{zero:.1f}" x2="{left + plot_w}" y2="{zero:.1f}" '
                 f'stroke="{SLATE}" stroke-width="1" opacity="0.7"/>')
    if y_label:
        parts.append(f'<text x="14" y="{top + plot_h / 2}" text-anchor="middle" font-size="10.5" '
                     f'fill="{MUTED}" transform="rotate(-90 14 {top + plot_h / 2})">{_esc(y_label)}</text>')

    for i, (label, value, err) in enumerate(zip(labels, values, errs)):
        x = left + step * i + (step - bar_w) / 2
        yv = py(value)
        y = min(zero, yv)
        h = abs(yv - zero)
        color = color_pos if value >= 0 else color_neg
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                     f'fill="{color}" opacity="0.85" rx="2"/>')
        if err:
            cx = x + bar_w / 2
            y1, y2 = py(value + err), py(value - err)
            parts.append(f'<line x1="{cx:.1f}" y1="{y1:.1f}" x2="{cx:.1f}" y2="{y2:.1f}" '
                         f'stroke="{INK}" stroke-width="1.3"/>')
            cap = bar_w * 0.28
            parts.append(f'<line x1="{cx - cap:.1f}" y1="{y1:.1f}" x2="{cx + cap:.1f}" y2="{y1:.1f}" '
                         f'stroke="{INK}" stroke-width="1.3"/>')
            parts.append(f'<line x1="{cx - cap:.1f}" y1="{y2:.1f}" x2="{cx + cap:.1f}" y2="{y2:.1f}" '
                         f'stroke="{INK}" stroke-width="1.3"/>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{top + plot_h + 18:.1f}" text-anchor="middle" '
                     f'font-size="10.5" fill="{MUTED}">{_esc(label)}</text>')
        if value_labels:
            ty = min(py(value + err) - 6, top + 8)
            if value < 0:
                ty = max(py(value - err) + 13, top + plot_h - 2)
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
                         f'font-size="10.5" fill="{color}">{_esc(formatter(value))}</text>')
    if caption:
        parts.append(f'<text x="{left}" y="{height - 8}" font-size="10.5" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def nav_chart(
    nav: Sequence[tuple[str, Sequence[float], str]],
    drawdown: Sequence[float],
    x_labels: Sequence[str],
    *,
    width: float = 980,
    height: float = 420,
    caption: str | None = None,
) -> str:
    """上：净值曲线（可多条）；下：回撤面积。"""
    left, right, top, gap, bottom = 64, 24, 26, 46, 30
    plot_w = width - left - right
    nav_h = (height - top - gap - bottom) * 0.66
    dd_top = top + nav_h + gap
    dd_h = height - dd_top - bottom

    all_values = [v for _, values, _ in nav for v in values if v == v]
    if not all_values:
        return f'<svg width="{width}" height="{height}"></svg>'
    y_lo, y_hi = min(all_values), max(all_values)
    pad = (y_hi - y_lo) * 0.08 or 0.05
    y_lo, y_hi = y_lo - pad, y_hi + pad

    def py(v: float) -> float:
        return top + nav_h - (v - y_lo) / ((y_hi - y_lo) or 1) * nav_h

    dd_lo = min(min(drawdown), -0.01) if drawdown is not None and len(drawdown) else -0.01
    def pdd(v: float) -> float:
        return dd_top + dd_h - (v - dd_lo) / ((0 - dd_lo) or 1) * dd_h

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')

    for i in range(4):
        value = y_lo + (y_hi - y_lo) * i / 3
        y = py(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                     f'fill="{MUTED}">{value:.2f}</text>')

    n = max(len(values) for _, values, _ in nav)
    for name, values, color in nav:
        pts = []
        for i, v in enumerate(values):
            if v == v:
                pts.append((left + plot_w * i / max(1, n - 1), py(v)))
        if len(pts) < 2:
            continue
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        lx, ly = pts[-1]
        parts.append(f'<text x="{lx + 5:.1f}" y="{ly - 5:.1f}" font-size="10" '
                     f'fill="{color}">{_esc(name)}</text>')

    # 回撤面板
    if drawdown is not None and len(drawdown):
        base = dd_top
        pts = [(left + plot_w * i / max(1, len(drawdown) - 1), pdd(v)) for i, v in enumerate(drawdown)]
        area = f"M {pts[0][0]:.1f},{base:.1f} L " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L {pts[-1][0]:.1f},{base:.1f} Z"
        parts.append(f'<path d="{area}" fill="{BURGUNDY}" opacity="0.16"/>')
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<path d="{d}" fill="none" stroke="{BURGUNDY}" stroke-width="1.2"/>')
        parts.append(f'<line x1="{left}" y1="{dd_top}" x2="{left + plot_w}" y2="{dd_top}" '
                     f'stroke="{RULE}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 9}" y="{dd_top + 10:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{MUTED}">0%</text>')
        parts.append(f'<text x="{left - 9}" y="{dd_top + dd_h - 2:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{MUTED}">{dd_lo:.0%}</text>')
        parts.append(f'<text x="{left + 4}" y="{dd_top + 12}" font-size="9.5" fill="{MUTED}">回撤</text>')

    if x_labels:
        for i, label in enumerate(x_labels):
            x = left + plot_w * i / max(1, len(x_labels) - 1)
            anchor = "start" if i == 0 else ("end" if i == len(x_labels) - 1 else "middle")
            parts.append(f'<text x="{x:.1f}" y="{height - 8:.1f}" text-anchor="{anchor}" '
                         f'font-size="10" fill="{MUTED}">{_esc(label)}</text>')
    if caption:
        parts.append(f'<text x="{left}" y="{dd_top - 6}" font-size="10" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _blend(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(a[k] + (b[k] - a[k]) * t):02x}" for k in range(3))


def heatmap(
    rows: Sequence[str],
    cols: Sequence[str],
    matrix: Sequence[Sequence[float]],
    *,
    cell_w: float = 52,
    cell_h: float = 30,
    row_label_w: float = 96,
    col_label_h: float = 26,
    caption: str | None = None,
    formatter=lambda v: f"{v:.2f}",
    vmax: float | None = None,
) -> str:
    width = row_label_w + cell_w * len(cols) + 20
    height = col_label_h + cell_h * len(rows) + (28 if caption else 12)
    limit = vmax or max([abs(v) for row in matrix for v in row if v == v] + [1e-9])
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')
    for j, col in enumerate(cols):
        x = row_label_w + j * cell_w + cell_w / 2
        parts.append(f'<text x="{x:.1f}" y="{col_label_h - 8}" text-anchor="middle" '
                     f'font-size="10.5" fill="{MUTED}">{_esc(col)}</text>')
    for i, row in enumerate(rows):
        y = col_label_h + i * cell_h
        parts.append(f'<text x="{row_label_w - 8}" y="{y + cell_h / 2 + 4}" text-anchor="end" '
                     f'font-size="11" fill="{INK}">{_esc(row)}</text>')
        for j, value in enumerate(matrix[i]):
            x = row_label_w + j * cell_w
            if value != value:
                fill, text_color = "#f6f7f9", FAINT
                label = "—"
            else:
                t = (value + limit) / (2 * limit)
                fill = _blend(TEAL, BURGUNDY, t)
                text_color = WHITE if abs(value) > limit * 0.55 else INK
                label = formatter(value)
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w - 2}" height="{cell_h - 2}" '
                         f'fill="{fill}" rx="2"/>')
            parts.append(f'<text x="{x + cell_w / 2 - 1:.1f}" y="{y + cell_h / 2 + 3.5:.1f}" '
                         f'text-anchor="middle" font-size="10" fill="{text_color}">{_esc(label)}</text>')
    if caption:
        parts.append(f'<text x="4" y="{height - 8}" font-size="10.5" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def score_bars(
    items: Sequence[tuple[str, float, float]],
    *,
    width: float = 980,
    height: float | None = None,
    caption: str | None = None,
) -> str:
    """评分条：items = (名称, 得分, 满分)。"""
    bar_h, gap = 22, 12
    height = height or (len(items) * (bar_h + gap) + 24)
    label_w, value_w = 168, 86
    plot_w = width - label_w - value_w - 16
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')
    for i, (name, score, full) in enumerate(items):
        y = 10 + i * (bar_h + gap)
        ratio = 0 if not full else max(0.0, min(1.0, score / full))
        parts.append(f'<text x="{label_w - 12}" y="{y + bar_h / 2 + 4}" text-anchor="end" '
                     f'font-size="12" fill="{INK}">{_esc(name)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{plot_w}" height="{bar_h}" '
                     f'fill="#eef1f4" rx="3"/>')
        color = NAVY if ratio >= 0.7 else (GOLD if ratio >= 0.45 else BURGUNDY)
        parts.append(f'<rect x="{label_w}" y="{y}" width="{plot_w * ratio:.1f}" height="{bar_h}" '
                     f'fill="{color}" opacity="0.85" rx="3"/>')
        parts.append(f'<text x="{label_w + plot_w + 10}" y="{y + bar_h / 2 + 4}" '
                     f'font-size="11.5" fill="{MUTED}">{score:.1f}/{full:.0f}</text>')
    if caption:
        parts.append(f'<text x="0" y="{height - 4}" font-size="10.5" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def quadrant(
    x: float,
    y: float,
    *,
    x_label: str = "时间稳定性",
    y_label: str = "空间区分度",
    width: float = 420,
    height: float = 420,
    threshold_x: float = 60.0,
    threshold_y: float = 60.0,
    caption: str | None = None,
) -> str:
    """二维象限图。中线画在实际阈值（默认 60），而不是几何中心 50。

    右上=核心型（双高）、左上=择时型（时间低/空间高）、
    右下=过滤型（时间高/空间低）、左下=淘汰（双低）。
    """
    pad = 52
    plot_w, plot_h = width - pad * 2, height - pad * 2
    tx = pad + plot_w * max(0.0, min(100.0, threshold_x)) / 100
    ty = pad + plot_h * (1 - max(0.0, min(100.0, threshold_y)) / 100)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')
    parts.append(f'<rect x="{pad}" y="{pad}" width="{plot_w}" height="{plot_h}" '
                 f'fill="#fafbfc" stroke="{RULE}"/>')
    # 阈值线（实际 60 分，不是 50）
    parts.append(f'<line x1="{tx:.1f}" y1="{pad}" x2="{tx:.1f}" '
                 f'y2="{pad + plot_h}" stroke="{SLATE}" stroke-dasharray="4 3" opacity="0.55"/>')
    parts.append(f'<line x1="{pad}" y1="{ty:.1f}" x2="{pad + plot_w}" '
                 f'y2="{ty:.1f}" stroke="{SLATE}" stroke-dasharray="4 3" opacity="0.55"/>')
    parts.append(f'<text x="{tx:.1f}" y="{pad + plot_h + 14}" text-anchor="middle" '
                 f'font-size="9" fill="{FAINT}">{threshold_x:.0f}</text>')
    parts.append(f'<text x="{pad - 6}" y="{ty + 3:.1f}" text-anchor="end" '
                 f'font-size="9" fill="{FAINT}">{threshold_y:.0f}</text>')

    # 四个区域标签（按实际阈值定位中心）
    regions = [
        ((threshold_x + 100) / 2, (threshold_y + 100) / 2, "核心型"),
        ((threshold_x + 0) / 2, (threshold_y + 100) / 2, "过滤型"),
        ((threshold_x + 0) / 2, (threshold_y + 0) / 2, "淘汰"),
        ((threshold_x + 100) / 2, (threshold_y + 0) / 2, "择时型"),
    ]
    for rx, ry, text in regions:
        px_ = pad + plot_w * rx / 100
        py_ = pad + plot_h * (1 - ry / 100)
        parts.append(f'<text x="{px_:.1f}" y="{py_:.1f}" font-size="10" fill="{FAINT}" '
                     f'text-anchor="middle">{_esc(text)}</text>')

    cx = pad + plot_w * min(1, max(0, x / 100))
    cy = pad + plot_h * (1 - min(1, max(0, y / 100)))
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="9" fill="{BURGUNDY}" opacity="0.18"/>')
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{BURGUNDY}"/>')
    parts.append(f'<text x="{cx:.1f}" y="{cy - 14:.1f}" font-size="10.5" fill="{INK}" '
                 f'text-anchor="middle">本因子（{x:.0f}, {y:.0f}）</text>')
    parts.append(f'<text x="{pad + plot_w / 2}" y="{height - 12}" text-anchor="middle" '
                 f'font-size="11" fill="{MUTED}">{_esc(x_label)} →</text>')
    parts.append(f'<text x="14" y="{pad + plot_h / 2}" text-anchor="middle" font-size="11" '
                 f'fill="{MUTED}" transform="rotate(-90 14 {pad + plot_h / 2})">{_esc(y_label)} →</text>')
    if caption:
        parts.append(f'<text x="{pad}" y="{pad - 12}" font-size="10.5" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def histogram(
    values: Sequence[float],
    *,
    bins: int = 24,
    width: float = 460,
    height: float = 200,
    color: str = NAVY,
    caption: str | None = None,
) -> str:
    clean = [v for v in values if v == v]
    if not clean:
        return f'<svg width="{width}" height="{height}"></svg>'
    lo, hi = min(clean), max(clean)
    span = (hi - lo) or 1.0
    counts = [0] * bins
    for v in clean:
        idx = min(bins - 1, int((v - lo) / span * bins))
        counts[idx] += 1
    cmax = max(counts) or 1
    left, right, top, bottom = 40, 12, 14, 30
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / bins
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<rect width="{width}" height="{height}" fill="{WHITE}"/>')
    for i, c in enumerate(counts):
        h = c / cmax * plot_h
        parts.append(f'<rect x="{left + i * step:.1f}" y="{top + plot_h - h:.1f}" '
                     f'width="{step - 1.5:.1f}" height="{h:.1f}" fill="{color}" opacity="0.75"/>')
    zero_x = left + (0 - lo) / span * plot_w if lo <= 0 <= hi else None
    if zero_x is not None:
        parts.append(f'<line x1="{zero_x:.1f}" y1="{top}" x2="{zero_x:.1f}" '
                     f'y2="{top + plot_h}" stroke="{SLATE}" stroke-width="1" stroke-dasharray="3 3"/>')
    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" '
                 f'y2="{top + plot_h}" stroke="{RULE}"/>')
    parts.append(f'<text x="{left}" y="{height - 10}" font-size="10" fill="{MUTED}">{_fmt(lo)}</text>')
    parts.append(f'<text x="{left + plot_w}" y="{height - 10}" text-anchor="end" '
                 f'font-size="10" fill="{MUTED}">{_fmt(hi)}</text>')
    if caption:
        parts.append(f'<text x="4" y="{height - 24}" font-size="10" fill="{FAINT}">'
                     f'{_esc(caption)}</text>')
    parts.append("</svg>")
    return "".join(parts)
