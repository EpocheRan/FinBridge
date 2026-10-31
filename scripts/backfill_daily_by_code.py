#!/usr/bin/env python
"""按代码回补 2022 年前日线（速度快一个数量级）。

为什么不用 ``sync_daily``
------------------------
``sync_daily`` 走「按交易日拉全市场」，每批 40 天 ≈ 14 万行，实测约 12 分钟/批，
回补 2018-2022（约 1150 个交易日）需要 5 小时以上。而研究只需要**点位池内的股票**，
所以改成「按代码拉全历史」：800 只 × 2400 行 ≈ 190 万行，约 10 分钟。

口径
----
- 用 Tushare ``daily``（不复权）+ ``adj_factor``；
- 写入 ``ohlcv_1d``，``market='MLCN'``，``(ts, code, market)`` 幂等；
- 已存在的日期会被 ``DEDUP`` 覆盖，可重复执行。

用法::

    python scripts/backfill_daily_by_code.py --pool-date 2017-11-01 --pool 800
    python scripts/backfill_daily_by_code.py --codes 600519,000001 --start 2017-01-01
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

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from data_interface.common.security_code import instrument_market, instrument_type  # noqa: E402

logger = logging.getLogger("Scripts.BackfillByCode")

_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "amount", "turnover", "adj_factor", "code", "market", "type"]


def main() -> int:
    parser = argparse.ArgumentParser(description="按代码回补历史日线")
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--pool", type=int, default=800)
    parser.add_argument("--codes", default=None, help="逗号分隔，指定时忽略 --pool-date")
    parser.add_argument("--start", default="2017-11-01")
    parser.add_argument("--end", default="2022-10-09")
    parser.add_argument("--sleep", type=float, default=0.22, help="每次调用间隔（限流）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = get_config()
    db = QuestDBClient(config)
    base = f"http://{resolve_questdb_host(config)}:{config['questdb'].get('rest_port', 9000)}"

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        resp = requests.get(
            f"{base}/exec",
            params={
                "query": f"select code from ohlcv_1d where ts='{args.pool_date}' and type <> 'idx' "
                f"order by amount desc limit {args.pool}"
            },
            timeout=120,
        ).json()
        codes = [row[0] for row in resp["dataset"]]
    logger.info("待回补 %s 只，区间 %s ~ %s", len(codes), args.start, args.end)

    import tushare as ts

    pro = ts.pro_api(config["tushare"]["token"])
    start_c = args.start.replace("-", "")
    end_c = args.end.replace("-", "")

    total = 0
    failed = 0
    for i, code in enumerate(codes, 1):
        suffix = "SH" if code.startswith(("6", "9")) else ("BJ" if code.startswith(("43", "83", "87", "92")) else "SZ")
        ts_code = f"{code}.{suffix}"
        try:
            raw = pro.daily(ts_code=ts_code, start_date=start_c, end_date=end_c)
            time.sleep(args.sleep)
            if raw is None or raw.empty:
                failed += 1
                continue
            factor = pro.adj_factor(ts_code=ts_code, start_date=start_c, end_date=end_c)
            time.sleep(args.sleep)
            raw = raw.rename(columns={"trade_date": "ts", "vol": "volume"})
            raw["ts"] = pd.to_datetime(raw["ts"], format="%Y%m%d").dt.strftime("%Y-%m-%d 00:00:00")
            raw["volume"] = (pd.to_numeric(raw["volume"], errors="coerce").fillna(0) * 100).astype("int64")
            raw["amount"] = pd.to_numeric(raw["amount"], errors="coerce").fillna(0.0) * 1000.0
            raw["turnover"] = 0.0
            raw["adj_factor"] = 1.0
            if factor is not None and not factor.empty:
                factor = factor.rename(columns={"trade_date": "ts"})
                factor["ts"] = pd.to_datetime(factor["ts"], format="%Y%m%d").dt.strftime("%Y-%m-%d 00:00:00")
                raw = raw.merge(factor[["ts", "adj_factor"]], on="ts", how="left", suffixes=("_x", ""))
                raw["adj_factor"] = pd.to_numeric(raw["adj_factor"], errors="coerce").fillna(1.0)
                raw = raw.drop(columns=[c for c in ("adj_factor_x",) if c in raw.columns])
            raw["code"] = code
            raw["market"] = raw["code"].map(instrument_market)
            raw["type"] = raw["code"].map(instrument_type)
            raw = raw.dropna(subset=["open", "high", "low", "close"])
            rows = [tuple(row[c] for c in _COLUMNS) for _, row in raw.iterrows()]
            if rows:
                sql = (
                    'INSERT INTO "ohlcv_1d" '
                    f"({', '.join(chr(34) + c + chr(34) for c in _COLUMNS)}) VALUES %s"
                )
                total += db.execute_values(sql, rows, page_size=500)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                logger.warning("%s 失败: %s", code, exc)
        if i % 100 == 0:
            logger.info("进度 %s/%s，累计写入 %s 行，失败 %s", i, len(codes), total, failed)

    logger.info("完成：写入 %s 行，失败 %s 只", total, failed)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
