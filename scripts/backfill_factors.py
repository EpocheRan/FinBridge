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
    parser.add_argument("--skip-done", action="store_true",
                        help="跳过已有 2019 年财务因子的代码（续跑）")
    parser.add_argument("--chunk", type=int, default=4, help="每批代码数（控制内存）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
    )
    register_all()
    end = args.end or today_str()

    # 用「覆盖足够多股票」的最新交易日取样：补数时可能给个别股票写入未来日期，
    # 直接取 max(ts) 会选到只有几只股票的那天。
    latest = _query(
        "select ts from (select ts, count() c from ohlcv_1d where market='MLCN' group by ts) "
        "where c > 3000 order by ts desc limit 1"
    )[0][0]
    rows = _query(
        f"""select code from ohlcv_1d where ts = '{latest}' and market='MLCN'
            order by amount desc limit {int(args.codes * 1.5)}"""
    )
    codes = [str(r[0]) for r in rows if str(r[0])[:2] in ("60", "00", "30", "68")][: args.codes]

    if getattr(args, "skip_done", False):
        done_rows = _query(
            "select distinct code from factor_value_daily "
            "where factor_id='quality.roe_ttm' and ts between '2019-01-01' and '2019-03-01'"
        )
        done = {str(r[0]) for r in done_rows}
        before = len(codes)
        codes = [c for c in codes if c not in done]
        logger.info("跳过已完成 %s 只，剩余 %s 只", before - len(codes), len(codes))
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
        result = None
        for attempt in range(3):
            try:
                result = service.build(ids, args.start, end, part)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("批次 %s 失败（第 %s 次）：%s", i // chunk + 1, attempt + 1,
                               str(exc)[:90])
                time.sleep(5 * (attempt + 1))
        if result is None:
            logger.error("批次 %s 放弃（%s 只）", i // chunk + 1, len(part))
            continue
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
