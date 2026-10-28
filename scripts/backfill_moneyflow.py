#!/usr/bin/env python
"""回补个股日频分档（moneyflow）。

用法：

    python scripts/backfill_moneyflow.py --start 2022-10-10 --end 2026-10-27
    python scripts/backfill_moneyflow.py --start 2024-01-01 --end 2024-01-31 --limit 5

特性：

- 先查库内已有交易日，自动跳过（可反复执行）；
- 逐交易日调用 Tushare ``moneyflow``（一次全市场），单位在 adapter 内换算；
- 单日失败/空数据只记录，不中断整体回补；
- 每 20 个交易日打印一次进度，适合后台运行。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # Windows 控制台默认 GBK
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd  # noqa: E402

from data_interface.common.config import get_config  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from data_interface.common.trading_calendar import TradingCalendar  # noqa: E402
from data_interface.moneyflow.service import MoneyflowService  # noqa: E402


def existing_days(db: QuestDBClient, start: str, end: str) -> set[str]:
    start_ts = datetime.combine(pd.Timestamp(start).date(), dt_time.min)
    end_ts = datetime.combine(pd.Timestamp(end).date(), dt_time.max).replace(microsecond=0)
    rows = db.query(
        'SELECT ts FROM "moneyflow_daily" WHERE ts BETWEEN %s AND %s GROUP BY ts',
        [start_ts, end_ts],
    )
    return {pd.Timestamp(row[0]).strftime("%Y-%m-%d") for row in rows or []}


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 moneyflow 日频分档")
    parser.add_argument("--start", default="2022-10-10")
    parser.add_argument("--end", default="2026-10-27")
    parser.add_argument("--sleep", type=float, default=0.02, help="每个交易日之间的间隔秒数")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个交易日（调试）")
    parser.add_argument("--reset", action="store_true", help="忽略库内已有日期，全部重拉")
    args = parser.parse_args()

    start = str(args.start)[:10]
    end = str(args.end)[:10]
    calendar = TradingCalendar()
    days = [str(day)[:10] for day in calendar.trading_days_or_weekdays(start, end)]
    db = QuestDBClient(get_config())
    existing = set() if args.reset else existing_days(db, start, end)
    todo = [day for day in days if day not in existing]
    if args.limit:
        todo = todo[: args.limit]

    print(
        f"[moneyflow] range={start}..{end} trading_days={len(days)} "
        f"existing={len(existing)} todo={len(todo)}",
        flush=True,
    )

    service = MoneyflowService()
    written = 0
    failed: list[dict[str, str]] = []
    started = time.monotonic()
    for index, day in enumerate(todo, 1):
        try:
            result = service.sync(day, day, max_trade_days=1)
        except Exception as exc:  # 单日异常不阻断整体回补
            failed.append({"date": day, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[moneyflow] {day} FAIL {type(exc).__name__}: {exc}", flush=True)
            continue
        rows = int(result.get("rows_written") or 0)
        written += rows
        if rows == 0:
            failed.append(
                {"date": day, "error": str(result.get("errors") or "empty")}
            )
        if index % 20 == 0 or index == len(todo):
            elapsed = time.monotonic() - started
            print(
                f"[moneyflow] {index}/{len(todo)} date={day} rows={rows} "
                f"total_rows={written} failed={len(failed)} elapsed={elapsed:.0f}s",
                flush=True,
            )
        if args.sleep:
            time.sleep(args.sleep)

    print(
        f"[moneyflow] DONE todo={len(todo)} rows={written} failed={len(failed)}",
        flush=True,
    )
    for item in failed[:20]:
        print(f"[moneyflow] failed {item['date']}: {item['error']}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
