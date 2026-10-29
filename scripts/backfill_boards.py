#!/usr/bin/env python
"""个股↔板块映射回补 + 自算板块日线。

用法::

    # 只刷映射（603699 → 16 个板块）
    python scripts/backfill_boards.py --codes 603699

    # 刷映射并自算所属板块近 30 个交易日收益
    python scripts/backfill_boards.py --codes 603699 --returns --start 2026-09-01

    # 全市场映射（5576 只 ≈ 20 分钟，慎用）
    python scripts/backfill_boards.py --all --max-codes 6000

设计说明

- 映射走东财 ``slist``（一股一请求），写入 PIT 表 ``map_stock_board``，
  同时补 ``dim_board``；**不复用** ``concept_member_snapshot``（那是同花顺 3000xx 命名空间）。
- 板块收益用成分股 + 流通市值自算，写回 ``concept_board_daily``，
  ``source='computed.bk'``，与外部的 THS 板块行情按 ``source`` 隔离、按 ``board_code`` 对齐。
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
from data_interface.concept.service import ConceptService  # noqa: E402

logger = logging.getLogger("Scripts.BackfillBoards")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="个股↔板块映射与板块日线自算")
    parser.add_argument("--codes", default="603699", help="逗号分隔的代码")
    parser.add_argument("--codes-file", default=None, help="每行一个代码的文件")
    parser.add_argument("--all", action="store_true", help="用库内全部 A 股代码")
    parser.add_argument("--max-codes", type=int, default=0, help=">0 时限制代码数")
    parser.add_argument("--as-of", default=None, help="快照日 YYYY-MM-DD，默认今天")
    parser.add_argument("--returns", action="store_true", help="额外自算板块日线")
    parser.add_argument("--start", default=None, help="板块日线起始日，默认近 30 个自然日")
    parser.add_argument("--end", default=None, help="板块日线结束日，默认今天")
    parser.add_argument("--weight", default="equal", choices=["equal", "float_mv"],
                        help="板块收益权重口径")
    parser.add_argument("--peers", action="store_true",
                        help="把代码扩展为「同行业个股」（spt=2），用于让行业板块有完整成分")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def load_codes(args: argparse.Namespace) -> list[str]:
    codes: list[str] = []
    if args.all:
        from data_interface.common.config import get_config
        from data_interface.common.questdb_client import QuestDBClient

        db = QuestDBClient(get_config())
        try:
            rows = db.query(
                'SELECT DISTINCT code FROM "ohlcv_1d" WHERE ts >= '
                "dateadd('d', -10, now()) ORDER BY code"
            ) or []
            codes.extend(str(row[0]) for row in rows if row and row[0])
        finally:
            db.close()
        logger.info("从 ohlcv_1d 取到 %s 个代码", len(codes))
    if args.codes_file:
        path = Path(args.codes_file)
        if not path.exists():
            raise SystemExit(f"代码文件不存在: {path}")
        codes.extend(path.read_text(encoding="utf-8").split())
    codes.extend(str(args.codes).replace("，", ",").split(","))

    cleaned: list[str] = []
    for item in codes:
        code = normalize_code(str(item).strip())
        if code and code not in cleaned:
            cleaned.append(code)
    if args.max_codes and len(cleaned) > args.max_codes:
        logger.warning("代码数 %s 超过 --max-codes，截断到 %s", len(cleaned), args.max_codes)
        cleaned = cleaned[: args.max_codes]
    return cleaned


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = load_config()
    codes = load_codes(args)
    if not codes:
        logger.error("没有可处理的代码")
        return 2

    service = ConceptService(config=config)

    if args.peers:
        expanded: list[str] = []
        for code in codes:
            peers = service.board_source.fetch_industry_peers(code)
            if peers:
                logger.info("%s 同行业个股 %s 只", code, len(peers))
                expanded.extend(peers)
            else:
                expanded.append(code)
        codes = list(dict.fromkeys(expanded))
        if args.max_codes and len(codes) > args.max_codes:
            logger.warning("同行业扩展后 %s 只超过 --max-codes，截断到 %s", len(codes), args.max_codes)
            codes = codes[: args.max_codes]

    result = service.sync_stock_boards(codes, as_of=args.as_of)
    logger.info(
        "映射写入：%s 只 → map_stock_board %s 行，dim_board %s 行，失败 %s 只",
        result["codes"],
        result["map_rows"],
        result["dim_rows"],
        len(result["errors"]),
    )
    if result["errors"]:
        logger.warning("失败样例: %s", result["errors"][:5])

    if args.returns:
        from data_interface.common.timeutil import now_cn

        start = args.start or (now_cn().date().replace(day=1)).isoformat()
        end = args.end or now_cn().date().isoformat()
        board_ids: list[str] = []
        for code in codes:
            frame = service.stock_boards(code, as_of=args.as_of)
            if frame.empty:
                continue
            board_ids.extend(str(item) for item in frame["board_id"].tolist())
        board_ids = sorted(set(board_ids))
        if not board_ids:
            logger.warning("没有可自算的板块")
            return 0
        logger.info("自算板块日线：%s 个板块，%s → %s", len(board_ids), start, end)
        summary = service.compute_board_daily(
            board_ids, start, end, weight=args.weight, as_of=args.as_of
        )
        logger.info("板块日线：%s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
