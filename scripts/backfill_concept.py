#!/usr/bin/env python
"""回补概念板块维度与日线。

用法：

    # 维度：当前 375 个板块投影回 2022-10-10 与 2026-10-27（重建快照）
    python scripts/backfill_concept.py --dataset dim

    # 日线：逐板块拉 2022-10-10..2026-10-27
    python scripts/backfill_concept.py --dataset daily --start 2022-10-10 --end 2026-10-27
    python scripts/backfill_concept.py --dataset daily --only-code 300008 --limit 2

注意：

- 没有历史板块列表/成分源，``dim`` 只能用当前列表做「重建快照」，
  ``source=akshare.ths_index.reconstructed``，存在幸存者偏差；
- 日线逐板块一次调用，单板块失败只记录不中断；可反复执行（DEDUP 幂等）。
"""

from __future__ import annotations

import argparse
import sys
import time
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

from data_interface.common.timeutil import now_cn  # noqa: E402
from data_interface.concept.models import BOARD_TYPE_CONCEPT  # noqa: E402
from data_interface.concept.service import ConceptService  # noqa: E402
from data_interface.concept.sources.ths_fast import ThsFastBoardSource  # noqa: E402

RECONSTRUCTED_SOURCE = "akshare.ths_index.reconstructed"


def backfill_dim(service: ConceptService, start: str, end: str) -> int:
    boards = service.boards()  # 当前最新快照的 375 个板块
    if not boards:
        print("[concept-dim] no boards; run concept_meta first", flush=True)
        return 0
    now = now_cn()
    frames = []
    for snapshot in (start, end):
        frames.append(
            pd.DataFrame(
                [
                    {
                        "ts": pd.Timestamp(snapshot),
                        "board_code": board.board_code,
                        "name": board.name,
                        "board_type": board.board_type or BOARD_TYPE_CONCEPT,
                        "source": RECONSTRUCTED_SOURCE,
                        "source_code": board.source_code or board.board_code,
                        "status": "active",
                        "updated_at": now,
                    }
                    for board in boards
                ]
            )
        )
    frame = pd.concat(frames, ignore_index=True)
    written = service.repository.upsert_boards(frame)
    print(
        f"[concept-dim] boards={len(boards)} snapshots=2 rows={written} "
        f"source={RECONSTRUCTED_SOURCE}",
        flush=True,
    )
    return written


def backfill_daily(
    service: ConceptService,
    start: str,
    end: str,
    *,
    only_code: str | None,
    only_codes: set[str] | None,
    limit: int | None,
    sleep_seconds: float,
) -> list[dict[str, str]]:
    boards = service.boards()
    if only_code:
        boards = [board for board in boards if board.board_code == only_code]
    if only_codes:
        boards = [board for board in boards if board.board_code in only_codes]
    if limit:
        boards = boards[:limit]
    print(
        f"[concept-daily] range={start}..{end} boards={len(boards)}",
        flush=True,
    )
    errors: list[dict[str, str]] = []
    written = 0
    started = time.monotonic()
    for index, board in enumerate(boards, 1):
        try:
            rows = service.sync_one_board_daily(board, start, end)
        except Exception as exc:
            errors.append({"board_code": board.board_code, "error": str(exc)})
            print(f"[concept-daily] {board.board_code} FAIL {exc}", flush=True)
            continue
        written += rows
        if rows == 0:
            errors.append({"board_code": board.board_code, "error": "empty"})
        if index % 20 == 0 or index == len(boards):
            elapsed = time.monotonic() - started
            print(
                f"[concept-daily] {index}/{len(boards)} code={board.board_code} "
                f"rows={rows} total_rows={written} failed={len(errors)} "
                f"elapsed={elapsed:.0f}s",
                flush=True,
            )
        if sleep_seconds:
            time.sleep(sleep_seconds)
    print(
        f"[concept-daily] DONE boards={len(boards)} rows={written} failed={len(errors)}",
        flush=True,
    )
    for item in errors[:20]:
        print(f"[concept-daily] failed {item['board_code']}: {item['error']}", flush=True)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="回补概念板块维度/日线")
    parser.add_argument("--dataset", choices=("dim", "daily"), required=True)
    parser.add_argument("--start", default="2022-10-10")
    parser.add_argument("--end", default="2026-10-27")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-code", default=None)
    parser.add_argument("--only-codes", default=None, help="逗号分隔的板块代码列表")
    args = parser.parse_args()

    service = ConceptService()
    if args.dataset == "dim":
        backfill_dim(service, args.start, args.end)
        return 0
    # 回补用快速通道：避免每个板块都刷新一遍「板块名称 -> 代码」映射
    service = ConceptService(daily_source=ThsFastBoardSource())
    only_codes = (
        {item.strip() for item in args.only_codes.split(",") if item.strip()}
        if args.only_codes
        else None
    )
    errors = backfill_daily(
        service,
        str(args.start)[:10],
        str(args.end)[:10],
        only_code=args.only_code,
        only_codes=only_codes,
        limit=args.limit,
        sleep_seconds=args.sleep,
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
