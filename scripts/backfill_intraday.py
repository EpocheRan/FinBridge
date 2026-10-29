#!/usr/bin/env python
"""分时（分钟线）回补脚本。

用法::

    python scripts/backfill_intraday.py --codes 603699,000001 --freq 5min
    python scripts/backfill_intraday.py --freq 1min --codes-file watchlist.txt
    python scripts/backfill_intraday.py --freq 5min --max-codes 300 --start 2026-10-01

设计约束（实测）

- 新浪分钟线单次上限 1023 根且**无区间参数**，只能拿到「截至最新」的数据：
  ``1min`` ≈ 4 个交易日、``5min`` ≈ 21 个交易日；因此本脚本定位是
  **每日增量 + 近端复核**，不是长历史回补。
- 东财 kline 主机在本机不可用、baostock 会挂起，所以默认源固定为 ``sina``；
  ``--source baostock`` 仅保留给其它网络环境且带超时熔断。
- 写库走 ``MarketDataFetcher.update_ohlc``，表内 ``DEDUP UPSERT KEYS(ts,code,market)``
  天然幂等，重复跑不会产生脏数据。
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
from data_interface.common.timeutil import today_str  # noqa: E402
from data_interface.market.constants import VALID_FREQUENCIES  # noqa: E402
from data_interface.market.fetcher import MarketDataFetcher  # noqa: E402

logger = logging.getLogger("Scripts.BackfillIntraday")

DEFAULT_CODES = "603699"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分钟线回补（新浪源）")
    parser.add_argument("--codes", default=DEFAULT_CODES, help="逗号分隔的代码")
    parser.add_argument("--codes-file", default=None, help="每行一个代码的文件")
    parser.add_argument("--freq", default="5min", choices=[f for f in VALID_FREQUENCIES if f.endswith("min")])
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（受源深度限制会被截断）")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--source", default="sina", help="数据源，默认 sina")
    parser.add_argument("--max-codes", type=int, default=0, help=">0 时限制股票数（防全市场误跑）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def resolve_codes(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.codes_file:
        path = Path(args.codes_file)
        if not path.exists():
            raise SystemExit(f"代码文件不存在: {path}")
        raw.extend(path.read_text(encoding="utf-8").split())
    raw.extend(str(args.codes).replace("，", ",").split(","))
    codes: list[str] = []
    for item in raw:
        code = normalize_code(item.strip())
        if code and code not in codes:
            codes.append(code)
    if args.max_codes and len(codes) > args.max_codes:
        logger.warning("代码数 %s 超过 --max-codes=%s，按前 %s 个执行", len(codes), args.max_codes, args.max_codes)
        codes = codes[: args.max_codes]
    return codes


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = load_config()
    end = args.end or today_str()
    start = args.start or end
    codes = resolve_codes(args)
    if not codes:
        logger.error("没有可回补的代码")
        return 2

    fetcher = MarketDataFetcher(source=args.source, config=config)
    total = 0
    failed: list[str] = []
    try:
        for code in codes:
            try:
                frame = fetcher.get_ohlc(code, start, end, args.freq)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s %s 拉取失败: %s", code, args.freq, exc)
                failed.append(code)
                continue
            if frame is None or frame.empty:
                logger.warning("%s %s 无数据", code, args.freq)
                failed.append(code)
                continue
            written = fetcher.update_ohlc(frame, args.freq)
            total += written
            logger.info(
                "%s %s 写入 %s 行（%s → %s）",
                code,
                args.freq,
                written,
                frame.index.min(),
                frame.index.max(),
            )
    finally:
        fetcher.close()

    logger.info("完成：%s 个代码，累计写入 %s 行，失败 %s 个", len(codes), total, len(failed))
    if failed:
        logger.warning("失败列表: %s", ", ".join(failed))
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
