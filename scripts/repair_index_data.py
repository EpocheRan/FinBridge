#!/usr/bin/env python
"""指数行情修复/补齐脚本。

背景
----
``ohlcv_1d`` 里长期只有 ``000300``（IDX）与 ``000001`` / ``000688``（被错标成 ``MLCN``），
**缺 399006 创业板指、000852 中证1000、399001 深证成指、000905 中证500**——
而“大盘状态开关”和 Beta 基准都依赖它们。

做法
----
1. **重贴标签**：把混在 ``MLCN``（股票）里的指数代码 ``UPDATE`` 成 ``IDX``，
   避免指数被当成个股进入横截面（QuestDB 的 ``UPDATE`` 对 WAL 表可用，实测生效）。
2. **补齐缺失**：用 ``IndexMarketSource``（Tushare → BaoStock → AkShare）拉取，
   统一以 ``market='IDX'`` 写入，``(ts, code, market)`` 幂等。
3. 只写入 ``ohlcv_1d``，不触碰因子表。

用法::

    python scripts/repair_index_data.py                # 重贴标签 + 补齐默认 8 个指数
    python scripts/repair_index_data.py --relabel-only # 只重贴标签
    python scripts/repair_index_data.py --codes 399006,000852 --start 2022-01-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd  # noqa: E402

from data_interface.common.config import load_config  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from data_interface.common.timeutil import today_str  # noqa: E402
from data_interface.market.sources.index_source import IndexMarketSource  # noqa: E402

logger = logging.getLogger("Scripts.RepairIndex")

#: 需要长期维护的指数（代码 -> 名称，仅用于日志）
DEFAULT_INDICES = {
    "000001": "上证指数",
    "000016": "上证50",
    "000300": "沪深300",
    "000688": "科创50",
    "000905": "中证500",
    "000852": "中证1000",
    "399001": "深证成指",
    "399006": "创业板指",
}

#: 历史遗留：被错标成 MLCN 的指数代码
MISLABELED_AS_STOCK = ("000001", "000688", "000016", "000300", "000905", "000852", "399001", "399006")

_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor", "code", "market"]


def _relabel(db: QuestDBClient) -> int:
    """把混在 MLCN 里的指数重贴为 IDX，返回处理的代码数。"""
    inlist = ",".join(f"'{code}'" for code in MISLABELED_AS_STOCK)
    rows = db.query(
        f'SELECT DISTINCT code FROM "ohlcv_1d" WHERE market = %s AND code IN ({inlist})',
        ("MLCN",),
    ) or []
    codes = [str(row[0]) for row in rows if row and row[0]]
    if not codes:
        logger.info("没有需要重贴标签的指数")
        return 0
    for code in codes:
        db.execute_http(
            f"UPDATE \"ohlcv_1d\" SET market='IDX' WHERE code='{code}' AND market='MLCN'"
        )
        logger.info("重贴标签 %s: MLCN -> IDX", code)
    return len(codes)


def _fetch_index(source: IndexMarketSource, code: str, start: date, end: date) -> pd.DataFrame:
    frame = source.fetch_ohlc(code, start, end)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame["code"] = code
    frame["market"] = "IDX"
    if "adj_factor" not in frame.columns:
        frame["adj_factor"] = 1.0
    if "turnover" not in frame.columns:
        frame["turnover"] = 0.0
    frame = frame.reset_index()
    if "ts" not in frame.columns:
        frame = frame.rename(columns={frame.columns[0]: "ts"})
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    for column in ("open", "high", "low", "close", "amount", "adj_factor", "turnover"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0).astype("int64")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame[_COLUMNS]


def main() -> int:
    parser = argparse.ArgumentParser(description="指数行情修复/补齐")
    parser.add_argument("--codes", default=",".join(DEFAULT_INDICES), help="逗号分隔的指数代码")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--relabel-only", action="store_true")
    parser.add_argument("--skip-relabel", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = load_config()
    db = QuestDBClient(config)
    source = IndexMarketSource()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end or today_str())

    try:
        if not args.skip_relabel:
            fixed = _relabel(db)
            logger.info("重贴标签完成：%s 个代码", fixed)
        if args.relabel_only:
            return 0

        total = 0
        for code in [c.strip() for c in args.codes.split(",") if c.strip()]:
            try:
                frame = _fetch_index(source, code, start, end)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 拉取失败: %s", code, exc)
                continue
            if frame.empty:
                logger.warning("%s 无数据", code)
                continue
            sql = (
                'INSERT INTO "ohlcv_1d" '
                f"({', '.join(chr(34) + c + chr(34) for c in _COLUMNS)}) VALUES %s"
            )
            rows = [tuple(row[c] for c in _COLUMNS) for _, row in frame.iterrows()]
            written = db.execute_values(sql, rows, page_size=500)
            total += written
            logger.info(
                "%s %s 写入 %s 行（%s -> %s）",
                code,
                DEFAULT_INDICES.get(code, ""),
                written,
                frame["ts"].iloc[0][:10],
                frame["ts"].iloc[-1][:10],
            )
        logger.info("合计写入 %s 行", total)

        summary = db.query(
            'SELECT code, count() c, min(ts) a, max(ts) b FROM "ohlcv_1d" '
            "WHERE market = 'IDX' GROUP BY code ORDER BY code"
        ) or []
        for code, count, first, last in summary:
            logger.info("  校验 %s: %s 行 %s ~ %s", code, count, str(first)[:10], str(last)[:10])
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
