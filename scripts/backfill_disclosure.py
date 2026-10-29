#!/usr/bin/env python
"""预约披露日历 / 公告列表回补。

用法::

    # 最近 4 个报告期的全市场预约披露日历（单期约 1117 页，pageSize=500）
    python scripts/backfill_disclosure.py --calendar

    # 指定报告期
    python scripts/backfill_disclosure.py --calendar --report-date 2026-09-30

    # 个股公告（默认近 1 页 100 条；--ann-pages 5 可拉 500 条）
    python scripts/backfill_disclosure.py --codes 603699 --ann-pages 5

    # 一次性：日历 + 一批股票的公告
    python scripts/backfill_disclosure.py --calendar --codes 603699,002438

说明：Tushare 的 ``disclosure_date`` 在本账号返回 0 行，故日历与公告都走东财接口。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from data_interface.common.config import load_config  # noqa: E402
from data_interface.common.security_code import normalize_code  # noqa: E402
from data_interface.disclosure.service import DisclosureService  # noqa: E402

logger = logging.getLogger("Scripts.BackfillDisclosure")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="预约披露日历 / 公告回补")
    parser.add_argument("--calendar", action="store_true", help="同步预约披露日历")
    parser.add_argument("--report-date", action="append", default=None,
                        help="报告期 YYYY-MM-DD，可重复；默认最近 4 个报告期")
    parser.add_argument("--codes", default=None, help="逗号分隔代码（公告）")
    parser.add_argument("--ann-pages", type=int, default=5, help="公告最多翻页数")
    parser.add_argument("--as-of", default=None, help="快照日 YYYY-MM-DD，默认今天")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    if not args.calendar and not args.codes:
        logger.error("至少要指定 --calendar 或 --codes")
        return 2

    service = DisclosureService(config=load_config())
    if args.calendar:
        result = service.sync_calendar(args.report_date, as_of=args.as_of)
        logger.info("日历写入 %s 行", result["rows_written"])
        for item in result["details"]:
            logger.info("  %s", item)

    if args.codes:
        codes = [normalize_code(item.strip()) for item in args.codes.replace("，", ",").split(",")]
        codes = [code for code in codes if code]
        result = service.sync_announcements(codes, max_pages=args.ann_pages)
        logger.info("公告写入 %s 行（%s 只）", result["rows_written"], result["codes"])
        if result["errors"]:
            logger.warning("失败样例: %s", result["errors"][:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
