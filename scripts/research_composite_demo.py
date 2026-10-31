#!/usr/bin/env python
"""多因子等权 rank 合成：真实数据端到端演示。

做三件事：
1. 从库里取一段日线 + 行业映射 + 流通市值，构造 ``date × symbol`` 参照面板；
2. 用 ``F.composite`` 合成一个「反转强度」分数（mom20 反向 0.5 + vol20 正向 0.3 +
   atr 反向 0.2），并做 **行业 + 市值中性化**；
3. 打印最新交易日的前 10 名，并给出每个分量的分位，便于人工核对。

用法::

    python scripts/research_composite_demo.py --pool 200 --days 120
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402
from factors import F, build_panel, required_warmup  # noqa: E402

logger = logging.getLogger("Scripts.CompositeDemo")

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"


def query(sql: str, timeout: int = 300) -> pd.DataFrame:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return pd.DataFrame(payload["dataset"], columns=[c["name"] for c in payload["columns"]])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=int, default=200)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--sync-boards", action="store_true", help="先补这批股票的板块归属")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    latest = query("select max(ts) m from ohlcv_1d where type <> 'idx'")["m"].iloc[0]
    start = (pd.Timestamp(latest) - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")
    logger.info("区间 %s ~ %s", start, latest)

    pool = query(
        f"""select code from ohlcv_1d where ts='{latest}' and type <> 'idx'
            order by amount desc limit {args.pool}"""
    )
    # 只保留股票（排除 ETF/LOF：51/15/16/58 开头）
    codes = [str(c) for c in pool["code"] if str(c)[:2] in ("60", "00", "30", "68")]
    inlist = ",".join(f"'{c}'" for c in codes)

    frame = query(
        f"""select ts, code, open, high, low, close, volume, amount, adj_factor
            from ohlcv_1d where type <> 'idx' and code in ({inlist}) and ts >= '{start}'"""
    )
    frame = frame.rename(columns={"ts": "date", "code": "symbol"})
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    logger.info("行情 %s 行 / %s 只", f"{len(frame):,}", frame["symbol"].nunique())

    if args.sync_boards:
        from data_interface.common.config import load_config
        from data_interface.concept.service import ConceptService

        service = ConceptService(config=load_config())
        result = service.sync_stock_boards(codes)
        logger.info("板块映射写入 %s 行（失败 %s）", result["map_rows"], len(result["errors"]))

    # 行业：取每只股票的主行业板块（is_primary 优先）
    board = query(
        f"""select code, board_name, is_primary from map_stock_board
            where code in ({inlist})
            and ts = (select max(ts) from map_stock_board)"""
    )
    if board.empty:
        logger.warning("map_stock_board 为空，跳过行业中性化")
        industry_map = {}
    else:
        board = board.sort_values(["code", "is_primary"], ascending=[True, False])
        industry_map = board.drop_duplicates("code").set_index("code")["board_name"]

    # 市值：流通市值
    valuation = query(
        f"""select ts, code, float_market_cap from fin_valuation_daily
            where code in ({inlist}) and ts >= '{start}'"""
    )
    valuation["ts"] = pd.to_datetime(valuation["ts"]).dt.tz_localize(None)
    valuation["float_market_cap"] = pd.to_numeric(valuation["float_market_cap"], errors="coerce")
    size_panel = valuation.pivot_table(index="ts", columns="code", values="float_market_cap")

    dates = sorted(frame["date"].unique())
    symbols = sorted(frame["symbol"].unique())
    if len(industry_map) > 0:
        industry_values = [industry_map.get(code, "unknown") for code in symbols]
        industry_panel = pd.DataFrame(
            [industry_values] * len(dates), index=pd.DatetimeIndex(dates), columns=symbols
        )
    else:
        industry_panel = None

    defs = [
        F.mom(20, name="mom20"),
        F.vol(20, name="vol20"),
        F.atr(14, name="atr14"),
        F.composite(
            [("mom20", -1, 0.5), ("vol20", 1, 0.3), ("atr14", -1, 0.2)],
            name="score.reversal",
            winsorize=0.02,
            neutralize=("industry", "size") if industry_panel is not None else None,
            min_parts=2,
        ),
    ]
    logger.info("预热窗口 %s 个交易日", required_warmup(defs))

    reference = {"industry": industry_panel, "size": size_panel} if industry_panel is not None else None
    panel = build_panel(
        frame,
        defs,
        start=pd.Timestamp(dates[0]).date(),
        end=pd.Timestamp(dates[-1]).date(),
        reference=reference,
    )

    score = panel.values["score.reversal"]
    last_dt = score.dropna(how="all").index.max()
    today = score.loc[last_dt].dropna().sort_values(ascending=False)
    print(f"\n=== {last_dt.date()} 反转强度 Top 10（等权 rank + 行业/市值中性化）===")
    print(f"{'代码':<8}{'行业':<12}{'分数':>7}{'mom20':>9}{'vol20':>9}{'atr14':>9}")
    for code in today.head(10).index:
        row = [f"{panel.values[k].loc[last_dt, code]:.3f}"
               for k in ("mom20", "vol20", "atr14")]
        print(f"{code:<8}{str(industry_map.get(code, 'unknown'))[:10]:<12}"
              f"{today[code]:>7.3f}{row[0]:>9}{row[1]:>9}{row[2]:>9}")

    print(f"\n覆盖：{today.notna().sum()} 只有效分数 / {len(symbols)} 只股票；"
          f"分数区间 {today.min():.3f} ~ {today.max():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
