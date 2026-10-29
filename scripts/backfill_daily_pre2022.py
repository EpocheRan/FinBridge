#!/usr/bin/env python
"""回补 2022-10 之前的日线（用于 2018/2020/2022 三轮熊市验证）。

复用 ``AShareSyncService.sync_daily``（Tushare ``daily`` 按交易日批量拉全市场，
自带复权因子与限流），只是把起始日提前。按批循环直到 ``done``。

用法::

    python scripts/backfill_daily_pre2022.py --start 2017-11-01 --end 2022-10-09
    python scripts/backfill_daily_pre2022.py --batch 40 --no-funds
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from data_interface.api.runtime import get_sync_service  # noqa: E402

logger = logging.getLogger("Scripts.BackfillDaily")


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 2022 年前日线")
    parser.add_argument("--start", default="2017-11-01")
    parser.add_argument("--end", default="2022-10-09")
    parser.add_argument("--batch", type=int, default=30, help="每批交易日数")
    parser.add_argument("--no-funds", action="store_true", help="跳过 ETF/LOF")
    parser.add_argument("--reset", action="store_true", help="重置断点")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    service = get_sync_service()
    cursor = None
    rounds = 0
    total_rows = 0
    stalled = 0
    while True:
        rounds += 1
        try:
            result = service.sync_daily(
                start_date=args.start,
                end_date=args.end,
                max_trade_days=args.batch,
                cursor=cursor,
                reset=args.reset and rounds == 1,
                include_funds=not args.no_funds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("第 %s 批失败: %s", rounds, exc)
            time.sleep(5)
            stalled += 1
            if stalled >= 3:
                return 2
            continue

        total_rows += int(result.get("rows_written") or 0)
        logger.info(
            "第 %s 批: 进度 %s/%s 写入 %s 行（累计 %s）errors=%s",
            rounds,
            result.get("processed"),
            result.get("total"),
            result.get("rows_written"),
            total_rows,
            len(result.get("errors") or []),
        )
        if result.get("done"):
            logger.info("完成：共 %s 批，累计写入 %s 行", rounds, total_rows)
            return 0
        cursor = result.get("next_cursor")
        if not cursor:
            logger.warning("没有 next_cursor，提前结束")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
