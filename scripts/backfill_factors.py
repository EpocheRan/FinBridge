#!/usr/bin/env python
"""财务因子物化回填（把 STORED 因子写入 factor_value_daily）。

背景
----
财务类 61 个因子标记为 ``materialization=STORED``，必须预计算落库，
否则回测/研究报告读不到（价量因子是 ``INLINE``，逐 bar 现算，不需要回填）。

用法::

    python scripts/backfill_factors.py --codes 100 --start 2025-01-01
    python scripts/backfill_factors.py --codes 300 --start 2024-01-01 --all
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

import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402
from data_interface.common.timeutil import today_str  # noqa: E402
from factor_engine.definitions import register_all  # noqa: E402
from factor_engine.fin_ext import FIN_EXT_SPECS  # noqa: E402
from factor_engine.fundamental import FUNDAMENTAL_SPECS  # noqa: E402
from factor_engine.service import FactorEngineService  # noqa: E402

logger = logging.getLogger("Scripts.BackfillFactors")

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"


def _query(sql: str) -> list:
    payload = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=120).json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["dataset"]


def main() -> int:
    parser = argparse.ArgumentParser(description="财务因子物化回填")
    parser.add_argument("--codes", type=int, default=120, help="按成交额取前 N 只")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--all", action="store_true", help="回填全部 STORED 因子（含既有 15 个成长/质量）")
    parser.add_argument("--chunk", type=int, default=4, help="每批代码数（控制内存）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
    )
    register_all()
    end = args.end or today_str()

    # 用库内最新交易日取样，避免今天无行情时选出 0 只
    latest = _query("select max(ts) from ohlcv_1d where market='MLCN'")[0][0]
    rows = _query(
        f"""select code from ohlcv_1d where ts = '{latest}' and market='MLCN'
            order by amount desc limit {int(args.codes * 1.5)}"""
    )
    codes = [str(r[0]) for r in rows if str(r[0])[:2] in ("60", "00", "30", "68")][: args.codes]
    logger.info("样本 %s 只，区间 %s ~ %s", len(codes), args.start, end)

    ids = [spec.factor_id for spec in FIN_EXT_SPECS]
    if args.all:
        ids += [spec.factor_id for spec in FUNDAMENTAL_SPECS]

    service = FactorEngineService()
    # 分批写：build 内部把每行拼成 dict 列表，全量一次会撑爆内存
    chunk = max(1, int(args.chunk))
    total_rows = 0
    started = time.time()
    for i in range(0, len(codes), chunk):
        part = codes[i : i + chunk]
        result = service.build(ids, args.start, end, part)
        written = int(result.get("rows_written") or 0)
        total_rows += written
        logger.info(
            "批次 %s/%s：%s 只，写入 %s 行（累计 %s）",
            i // chunk + 1, (len(codes) + chunk - 1) // chunk, len(part), written, total_rows,
        )
    elapsed = time.time() - started
    logger.info(
        "回填完成：%s 个因子 × %s 只，共写入 %s 行，耗时 %.1fs",
        len(ids), len(codes), total_rows, elapsed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
