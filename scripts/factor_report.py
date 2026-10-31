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


def _declaration_block(meta: dict, factor: str) -> str:
    """报告「只增不减」：profile 概览 + 数据完整性申明 + 未启用项。"""
    profile = meta["profile"]
    rows = "".join(
        f"<tr><td>{d['dataset']}</td><td>{d['start']}</td><td>{d['end']}</td><td>{d['status']}</td></tr>"
        for d in meta["domains"]
    )
    not_selected = "、".join(meta["not_selected"]) or "（无）"
    items = "、".join(meta["items"])
    statuses = meta.get("item_status", {}).get(factor, {})
    bad = [f"{key}: {value[1]}" for key, value in statuses.items() if value[0] != "ok" and value[1]]
    warn = "<br>".join(bad) if bad else "无"
    return f"""
<section style="max-width:1180px;margin:24px auto;padding:18px 22px;border:1px solid #d8dee9;
border-radius:10px;background:#fafbfe;font:14px/1.7 -apple-system,'Segoe UI','PingFang SC',sans-serif;color:#26303d">
  <div style="font-size:12px;letter-spacing:2px;color:#3b6ea5;font-weight:700">EVALUATION PROFILE</div>
  <h2 style="margin:6px 0 10px;font-size:20px">评估口径：{profile['name']} v{profile['version']}</h2>
  <p style="margin:0 0 10px;color:#4a5568">评估项（{len(meta['items'])}）：{items}</p>
  <p style="margin:0 0 12px;color:#4a5568">本 profile 未启用：{not_selected}</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="text-align:left;color:#6b7684">
      <th style="padding:6px 8px;border-bottom:1px solid #e2e8f0">数据集</th>
      <th style="padding:6px 8px;border-bottom:1px solid #e2e8f0">起</th>
      <th style="padding:6px 8px;border-bottom:1px solid #e2e8f0">止</th>
      <th style="padding:6px 8px;border-bottom:1px solid #e2e8f0">状态</th>
    </tr></thead><tbody>{rows}</tbody>
  </table>
  <p style="margin:12px 0 0;color:#8a5a00"><b>降级/不可用项：</b>{warn}</p>
  <p style="margin:6px 0 0;color:#8b98ad;font-size:12px">数据空缺在报告内申明，不删减报告正文。</p>
</section>
"""


def _inject(html_path: Path, block: str) -> bool:
    text = html_path.read_text(encoding="utf-8")
    if "EVALUATION PROFILE" in text:
        return False
    marker = "<body>"
    if marker in text:
        text = text.replace(marker, marker + block, 1)
    else:
        text = text.replace("</body>", block + "</body>", 1)
    html_path.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="因子研究报告生成器")
    parser.add_argument("--factor", default="momentum.return_20d")
    parser.add_argument("--batch", default=None, help="逗号分隔，批量生成")
    parser.add_argument("--universe", default="all_a", help="all_a | index:000905 | pool")
    parser.add_argument("--pool", type=int, default=200)
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--out", default="output/factor_reports")
    parser.add_argument("--category", default=None)
    parser.add_argument("--direction", type=int, default=None)
    parser.add_argument("--profile", default="default", help="评估 profile（决定评估项）")
    parser.add_argument("--no-declaration", action="store_true", help="不注入 profile/完整性申明区块")
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
        if not args.no_declaration:
            try:
                from research.pipeline import run as _run_pipeline

                _, meta = _run_pipeline(
                    [factor],
                    profile=args.profile,
                    universe=f"pool:{args.pool_date}:{args.pool}",
                    start=args.start,
                    end=args.end,
                    rating_from_report=rating,
                )
                if _inject(Path(result["html"]), _declaration_block(meta, factor)):
                    print(f"    已注入 profile「{meta['profile']['name']}」与数据完整性申明")
            except Exception as exc:  # noqa: BLE001 - 申明失败不影响报告正文
                print(f"    申明注入失败（报告正文不受影响）: {type(exc).__name__}: {exc}")
        if args.png:
            png = Path(args.out) / (factor.replace('.', '_').replace('@', '_') + ".png")
            if shoot(result["html"], png, height=args.height):
                print(f"    预览图: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
