"""因子报告编排：取数 → 计算 → 评级 → 渲染。

设计约束
--------
- **不引入绘图第三方库**：图全部是内联 SVG，保证打印/PDF 无损；
- **口径固定**：T+1 开盘买入、双边 20bp、逐日截面统计；
- **可复现**：``run_id`` 记录数据版本与参数，重跑结果一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from data_interface.common.config import get_config, resolve_questdb_host
from data_interface.common.timeutil import now_cn

from . import metrics, rating as rating_mod, render

logger = logging.getLogger("Research.Report")

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"


def _query(sql: str, timeout: int = 300) -> pd.DataFrame:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return pd.DataFrame(payload["dataset"], columns=[c["name"] for c in payload["columns"]])


# ---------------------------------------------------------------- 因子库（内联计算）
def compute_factor(factor_id: str, close: pd.DataFrame) -> pd.DataFrame:
    """支持 ``momN`` / ``momentum.ret@N`` / ``reversal.short@N`` / ``volatility.Nd``。"""
    key = str(factor_id).strip()
    rets = close.pct_change(fill_method=None)

    if key.startswith("mom") and key[3:].isdigit():
        n = int(key[3:])
        return close / close.shift(n) - 1
    if key.startswith("momentum.ret@"):
        n = int(key.split("@")[1])
        return close / close.shift(n) - 1
    if key.startswith("reversal.short@"):
        n = int(key.split("@")[1])
        return -(close / close.shift(n) - 1)
    if key.startswith("volatility.") and key.endswith("d"):
        n = int(key.split(".")[1][:-1])
        return rets.rolling(n).std()
    raise ValueError(f"未知因子: {factor_id}")


# ---------------------------------------------------------------- 面板装载
@dataclass
class Panel:
    close: pd.DataFrame
    open: pd.DataFrame
    market_cap: pd.DataFrame | None
    industry: pd.DataFrame | None
    regime: pd.Series | None
    symbols: list[str]


def load_panel(pool_date: str, pool_size: int, start: str, end: str, *, min_amount: float | None = None, max_amount: float | None = None) -> Panel:
    amount_filter = ""
    if min_amount is not None:
        amount_filter += f" and amount >= {min_amount}"
    if max_amount is not None:
        amount_filter += f" and amount <= {max_amount}"
    pool = _query(
        f"""select code from ohlcv_1d where ts='{pool_date}' and market='MLCN'{amount_filter}
            order by amount desc limit {int(pool_size * 1.6)}"""
    )
    codes = [str(c) for c in pool["code"] if str(c)[:2] in ("60", "00", "30", "68")][:pool_size]
    inlist = ",".join(f"'{c}'" for c in codes)

    frame = _query(
        f"""select ts, code, open, close from ohlcv_1d
            where market='MLCN' and code in ({inlist}) and ts between '{start}' and '{end}'"""
    )
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    close = frame.pivot_table(index="ts", columns="code", values="close").sort_index()
    open_ = frame.pivot_table(index="ts", columns="code", values="open").sort_index()

    cap = _query(
        f"""select ts, code, float_market_cap from fin_valuation_daily
            where code in ({inlist}) and ts between '{start}' and '{end}'"""
    )
    market_cap = None
    if not cap.empty:
        cap["ts"] = pd.to_datetime(cap["ts"]).dt.tz_localize(None)
        cap["float_market_cap"] = pd.to_numeric(cap["float_market_cap"], errors="coerce")
        market_cap = cap.pivot_table(index="ts", columns="code", values="float_market_cap")

    industry = None
    board = _query(
        f"""select code, board_name, is_primary from map_stock_board
            where code in ({inlist}) and ts=(select max(ts) from map_stock_board)"""
    )
    if not board.empty:
        board = board.sort_values(["code", "is_primary"], ascending=[True, False])
        mapping = board.drop_duplicates("code").set_index("code")["board_name"]
        cols = list(close.columns)
        industry = pd.DataFrame(
            [[mapping.get(c, "未知") for c in cols]] * len(close.index),
            index=close.index, columns=cols,
        )

    regime = None
    idx = _query("select ts, close from ohlcv_1d where code='000852' and market='IDX' order by ts")
    if not idx.empty:
        idx["ts"] = pd.to_datetime(idx["ts"]).dt.tz_localize(None)
        idx["close"] = pd.to_numeric(idx["close"])
        idx["mom20"] = idx["close"] / idx["close"].shift(20) - 1
        idx["ma20"] = idx["close"].rolling(20).mean()
        idx["regime"] = np.where(
            (idx["mom20"] > 0) & (idx["close"] > idx["ma20"]), "bull",
            np.where((idx["mom20"] < -0.03) & (idx["close"] < idx["ma20"]), "bear", "neutral"),
        )
        regime = idx.set_index("ts")["regime"].reindex(close.index).ffill()

    return Panel(close=close, open=open_, market_cap=market_cap, industry=industry,
                 regime=regime, symbols=list(close.columns))


# ---------------------------------------------------------------- 生成报告
def generate(
    factor_id: str,
    *,
    pool_date: str = "2017-11-01",
    pool_size: int = 700,
    start: str = "2018-01-01",
    end: str | None = None,
    universe: str = "点位池 700（2017-11 成交额）",
    category: str = "momentum",
    direction: int = 1,
    out_dir: str | Path = "output/reports",
) -> dict:
    end = end or now_cn().date().isoformat()
    panel = load_panel(pool_date, pool_size, start, end)
    factor = compute_factor(factor_id, panel.close)

    report = metrics.build_report(
        factor, panel.close, panel.open,
        factor_id=factor_id, category=category, direction=direction,
        universe=universe,
        regime=panel.regime, market_cap=panel.market_cap, industry=panel.industry,
    )
    score = rating_mod.rate(report)

    run_hash = hashlib.sha1(
        f"{factor_id}|{start}|{end}|{pool_size}|{len(panel.close)}".encode()
    ).hexdigest()[:8]
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "run_id": f"{now_cn().strftime('%Y%m%dT%H%M')}_{run_hash}",
        "data_version": f"ohlcv_1d:{panel.close.index.max().date()}",
    }

    html = render.render(report, score, meta)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    safe = factor_id.replace(".", "_").replace("@", "_")
    html_path = target / f"{safe}.html"
    html_path.write_text(html, encoding="utf-8")

    payload = {
        "run_id": meta["run_id"],
        "factor": factor_id,
        "rating": {k: v for k, v in score.items() if k != "items"},
        "ic_stats": report.ic_stats.round(4).to_dict(orient="index"),
        "warnings": report.warnings,
    }
    json_path = target / f"{safe}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("报告已生成: %s（评级 %s / %s 分）", html_path, score["grade"], score["score"])
    return {"html": html_path, "json": json_path, "rating": score, "report": report, "meta": meta}
