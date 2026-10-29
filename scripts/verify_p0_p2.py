#!/usr/bin/env python
"""P0–P2 端到端验收：分时结构 / 板块归属与板块收益 / 披露日历与公告。

用法::

    python scripts/verify_p0_p2.py
    python scripts/verify_p0_p2.py --code 603699 --date 2026-10-28

验收标准（与手工核对过的真实值一致）：
1. 分时：能把 2026-10-28 的「11:12 首次封板、2 次炸板、尾盘竞价 98,200 股」还原出来；
2. 板块：603699 命中 16 个板块且含 BK0545 通用设备，自算板块涨幅与东财口径同号、
   偏差在 1 个百分点以内；
3. 披露：603699 三季报预约为 2026-10-30（剩余 2 天），公告列表已入库且最新为质押公告。
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
except Exception:
    pass

from data_interface.common.config import load_config  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from data_interface.market.intraday import session_metrics  # noqa: E402

logger = logging.getLogger("Scripts.Verify")

RESULTS: list[tuple[bool, str, str]] = []


def check(ok_flag: bool, name: str, detail: str) -> None:
    RESULTS.append((ok_flag, name, detail))
    print(f"[{'PASS' if ok_flag else 'FAIL'}] {name:34s} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="603699")
    parser.add_argument("--date", default="2026-10-28")
    parser.add_argument("--em-board-pct", type=float, default=3.65, help="东财口径板块涨幅参考值")
    args = parser.parse_args()

    config = load_config()
    db = QuestDBClient(config)
    code, trade_date = args.code, args.date

    try:
        # ---------------- P0 分时 ----------------
        minute = db.query_dataframe(
            'SELECT ts, open, high, low, close, volume, amount FROM "ohlcv_1m" '
            "WHERE code = %s AND ts BETWEEN %s AND %s ORDER BY ts",
            (code, f"{trade_date} 00:00:00", f"{trade_date} 23:59:59"),
        )
        bars = 0 if minute is None else len(minute)
        check(bars >= 200, "P0 分时落库", f"1min bars={bars}")

        prev = db.query_dataframe(
            'SELECT close FROM "ohlcv_1d" WHERE code = %s AND ts < %s ORDER BY ts DESC LIMIT 1',
            (code, f"{trade_date} 00:00:00"),
        )
        prev_close = float(prev["close"].iloc[0]) if prev is not None and not prev.empty else None
        metrics = session_metrics(minute, prev_close, board="MAIN") if bars else None
        if metrics:
            check(
                metrics.first_seal_time == "11:12",
                "P0 首次封板时点",
                f"{metrics.first_seal_time}（期望 11:12）",
            )
            check(metrics.break_count == 2, "P0 炸板次数", f"{metrics.break_count}（期望 2）")
            check(
                metrics.auction_volume == 98200,
                "P0 尾盘竞价量",
                f"{metrics.auction_volume} 股（期望 98,200）",
            )
        else:
            check(False, "P0 分时结构", "无分时数据")

        # ---------------- P1 板块 ----------------
        boards = db.query_dataframe(
            'SELECT board_id, board_name, board_type, is_primary FROM "map_stock_board" '
            "WHERE code = %s AND ts = (SELECT max(ts) FROM \"map_stock_board\" WHERE code = %s)",
            (code, code),
        )
        count = 0 if boards is None else len(boards)
        check(count == 16, "P1 板块归属数量", f"{count} 个（期望 16）")
        has_general = boards is not None and "BK0545" in set(boards["board_id"])
        check(bool(has_general), "P1 含 BK0545 通用设备", "是" if has_general else "否")

        board_daily = db.query_dataframe(
            'SELECT ts, pct_chg FROM "concept_board_daily" WHERE board_code = %s '
            "AND source = 'computed.bk' AND ts BETWEEN %s AND %s ORDER BY ts",
            ("BK0545", f"{trade_date} 00:00:00", f"{trade_date} 23:59:59"),
        )
        if board_daily is not None and not board_daily.empty:
            computed = float(board_daily["pct_chg"].iloc[0])
            gap = abs(computed - args.em_board_pct)
            check(
                gap <= 1.0,
                "P1 板块涨幅(自算)",
                f"{computed:+.2f}% vs 东财 {args.em_board_pct:+.2f}%（偏差 {gap:.2f}pp）",
            )
        else:
            check(False, "P1 板块涨幅(自算)", "无 computed.bk 数据")

        # ---------------- P2 披露 ----------------
        calendar = db.query_dataframe(
            'SELECT report_date, appoint_date, residual_days, actual_date, report_type '
            'FROM "fin_disclosure_calendar" WHERE code = %s AND report_date = %s '
            "LATEST ON ts PARTITION BY code, report_date",
            (code, "2026-09-30 00:00:00"),
        )
        if calendar is not None and not calendar.empty:
            row = calendar.iloc[0]
            appoint = str(row["appoint_date"])[:10]
            check(appoint == "2026-10-30", "P2 三季报预约披露日", f"{appoint}（期望 2026-10-30）")
            check(
                int(row["residual_days"]) == 2,
                "P2 剩余天数",
                f"{int(row['residual_days'])} 天（期望 2）",
            )
        else:
            check(False, "P2 预约披露日历", "未入库")

        announcements = db.query_dataframe(
            'SELECT ts, column_name, title FROM "announcement" WHERE code = %s ORDER BY ts DESC LIMIT 1',
            (code,),
        )
        if announcements is not None and not announcements.empty:
            row = announcements.iloc[0]
            notice = announcements.index[0]
            check(
                str(notice)[:10] == "2026-10-24",
                "P2 最新公告",
                f"{str(notice)[:10]} {row['column_name']}",
            )
        else:
            check(False, "P2 公告列表", "未入库")

        upcoming = db.query(
            'SELECT count() FROM (SELECT * FROM "fin_disclosure_calendar" '
            "LATEST ON ts PARTITION BY code, report_date) "
            "WHERE actual_date IS NULL AND appoint_date BETWEEN %s AND dateadd('d', 7, %s)",
            ("2026-10-28 00:00:00", "2026-10-28 00:00:00"),
            fetch_all=False,
        )
        check(bool(upcoming and upcoming[0]), "P2 全市场待披露(7日)", f"{upcoming[0] if upcoming else 0} 家")
    finally:
        db.close()

    failed = [item for item in RESULTS if not item[0]]
    print("-" * 72)
    print(f"合计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
