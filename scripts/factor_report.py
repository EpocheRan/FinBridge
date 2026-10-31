#!/usr/bin/env python
"""生成因子研究报告（HTML，可选 PNG 预览）。

用法::

    python scripts/factor_report.py --factor momentum.ret@20
    python scripts/factor_report.py --factor reversal.short@5 --pool 500 --start 2019-01-01
    python scripts/factor_report.py --batch mom20,mom60,volatility.20d --png
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from research.report.runner import generate  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def shoot(html_path: Path, png_path: Path, width: int = 1180, height: int = 2600) -> bool:
    if not Path(CHROME).exists():
        return False
    try:
        subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--window-size={width},{height}", f"--screenshot={png_path}",
             html_path.resolve().as_uri()],
            check=False, capture_output=True, timeout=120,
        )
        return png_path.exists()
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="因子研究报告生成器")
    parser.add_argument("--factor", default="momentum.return_20d")
    parser.add_argument("--batch", default=None, help="逗号分隔，批量生成")
    parser.add_argument("--pool", type=int, default=200)
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--out", default="output/factor_reports")
    parser.add_argument("--category", default=None)
    parser.add_argument("--direction", type=int, default=None)
    parser.add_argument("--png", action="store_true", help="同时用 headless Chrome 截图")
    parser.add_argument("--height", type=int, default=2600, help="截图高度（像素）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    factors = [f.strip() for f in (args.batch or args.factor).split(",") if f.strip()]

    for factor in factors:
        result = generate(
            factor,
            pool_date=args.pool_date,
            pool_size=args.pool,
            start=args.start,
            end=args.end,
            universe=f"点位池 {args.pool}（{args.pool_date} 成交额）",
            category=args.category,
            direction=args.direction,
            out_dir=args.out,
        )
        rating = result["rating"]
        print(f"[{factor}] 评级 {rating['grade']} / {rating['score']} 分 "
              f"（时间 {rating['time_score']:.0f} × 空间 {rating['space_score']:.0f}）"
              f" → {result['html']}")
        for veto in rating["vetoes"]:
            print(f"    否决: {veto}")
        if args.png:
            png = Path(args.out) / (factor.replace('.', '_').replace('@', '_') + ".png")
            if shoot(result["html"], png, height=args.height):
                print(f"    预览图: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
