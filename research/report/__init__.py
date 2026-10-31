"""因子研究报告：可视化 PDF 级 HTML + 评级。

模块结构::

    research/report/
    ├── charts.py       纯 SVG 图表原语（折线/柱状/热力/评分条/象限/直方图）
    ├── metrics.py      指标计算（IC/ICIR/NW-t/分层/衰减/换手/分组 IC）
    ├── diagnostics.py  断言（ρ↔价差/IC→价差/成本区间/方向先验）、失效归因、家族
    ├── rating.py       100 分制评分卡 + 一票否决 + 时间×空间四象限
    ├── render.py       教科书式 HTML 排版（内联 CSS + SVG，可直接打印）
    └── runner.py       编排：取数 → 计算 → 评级 → 渲染 → 落盘
"""

from . import charts, diagnostics, metrics, rating, render, runner
from .runner import generate

__all__ = ["charts", "diagnostics", "metrics", "rating", "render", "runner", "generate"]
