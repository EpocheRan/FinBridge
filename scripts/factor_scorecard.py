#!/usr/bin/env python
"""全量因子评分卡：批量跑评级并写入 ``factor_scorecard``。

用法::

    python scripts/factor_scorecard.py --source price --pool 500
    python scripts/factor_scorecard.py --source price --only momentum --render
    python scripts/factor_scorecard.py --source financial --codes 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

import gc  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from data_interface.common.timeutil import now_cn  # noqa: E402
from research.report import definitions, diagnostics as diag, metrics, rating as rating_mod, render, scorecard  # noqa: E402
from research.report.definitions import peers_for  # noqa: E402
from research.report.runner import _query, build_payload, load_panel  # noqa: E402

logger = logging.getLogger("Scripts.Scorecard")

OUT_DIR = Path("output/factor_reports")


def _pivot(sql: str, column: str) -> pd.DataFrame:
    frame = _query(sql)
    if frame.empty:
        return pd.DataFrame()
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    return frame.pivot_table(index="ts", columns="code", values=column, aggfunc="last")


def _bench(code: str, start: str, end: str) -> pd.Series:
    frame = _query(
        "select ts, close from ohlcv_1d "
        f"where type='idx' and code='{code}' and ts between '{start}' and '{end}' order by ts"
    )
    if frame.empty:
        return pd.Series(dtype="float64")
    return pd.Series(
        pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame["ts"]).dt.tz_localize(None),
    )


def _bench_volume(code: str, start: str, end: str) -> pd.Series:
    frame = _query(
        "select ts, volume from ohlcv_1d "
        f"where type='idx' and code='{code}' and ts between '{start}' and '{end}' order by ts"
    )
    if frame.empty:
        return pd.Series(dtype="float64")
    return pd.Series(
        pd.to_numeric(frame["volume"], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame["ts"]).dt.tz_localize(None),
    )


def _load_raw(pool: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    codes = ",".join(f"'{c}'" for c in pool)
    base = f"from ohlcv_1d where type='stock' and ts between '{start}' and '{end}' and code in ({codes})"
    return {
        "high": _pivot(f"select ts, code, high {base}", "high"),
        "low": _pivot(f"select ts, code, low {base}", "low"),
        "volume": _pivot(f"select ts, code, volume {base}", "volume"),
        "amount": _pivot(f"select ts, code, amount {base}", "amount"),
        "turnover": _pivot(f"select ts, code, turnover {base}", "turnover"),
        "close_raw": _pivot(f"select ts, code, close {base}", "close"),
        "bench_csi300": _bench("000300", start, end),
        "bench_sh": _bench("000001", start, end),
        "bench_vol_csi300": _bench_volume("000300", start, end),
        "bench_vol_sh": _bench_volume("000001", start, end),
    }



def _load_price_panel(codes: list[str], start: str, end: str):
    """按给定代码直接取后复权 close/open（财务因子样本与点位池可能不重合）。"""
    chunk = ",".join(f"'{c}'" for c in codes)
    frame = _query(
        "select ts, code, close, open, adj_factor from ohlcv_1d "
        f"where type='stock' and ts between '{start}' and '{end}' and code in ({chunk})",
        timeout=600,
    )
    if frame.empty:
        return pd.DataFrame(), pd.DataFrame()
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    for column in ("close", "open", "adj_factor"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["adj_factor"] = frame.groupby("code")["adj_factor"].transform("last")
    factor = frame["adj_factor"].fillna(1.0)
    frame["close_hfq"] = frame["close"] * factor
    frame["open_hfq"] = frame["open"] * factor
    close = frame.pivot_table(index="ts", columns="code", values="close_hfq", aggfunc="last")
    open_ = frame.pivot_table(index="ts", columns="code", values="open_hfq", aggfunc="last")
    return close.sort_index(), open_.reindex(index=close.index, columns=close.columns)


def _financial_panel(factor_id: str, close: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """从 factor_value_daily（物化结果）取单因子面板。"""
    codes = ",".join(f"'{c}'" for c in close.columns)
    frame = _query(
        "select ts, code, value from factor_value_daily "
        f"where factor_id='{factor_id}' and ts between '{start}' and '{end}' "
        f"and code in ({codes})",
        timeout=600,
    )
    if frame.empty:
        return pd.DataFrame()
    frame["ts"] = pd.to_datetime(frame["ts"]).dt.tz_localize(None)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    wide = frame.pivot_table(index="ts", columns="code", values="value", aggfunc="last")
    return wide.reindex(index=close.index, columns=close.columns)


def _factor_panel(factor_id: str, panel, raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """按 symbol 分组计算因子，返回 date × symbol 面板。"""
    from factors.ops.price_factors import lookup

    fn, _window, _unit = lookup(factor_id)
    close = panel.close
    ratio = (close / raw["close_raw"].reindex_like(close)).replace([np.inf, -np.inf], np.nan)
    frame = pd.DataFrame(
        {
            "ts": np.repeat(close.index.values, close.shape[1]),
            "symbol": np.tile(close.columns.values, close.shape[0]),
            "close": close.to_numpy().ravel(),
            "hfq_close": close.to_numpy().ravel(),
            "open": panel.open.to_numpy().ravel(),
            "high": (raw["high"].reindex_like(close) * ratio).to_numpy().ravel(),
            "low": (raw["low"].reindex_like(close) * ratio).to_numpy().ravel(),
            "hfq_high": (raw["high"].reindex_like(close) * ratio).to_numpy().ravel(),
            "hfq_low": (raw["low"].reindex_like(close) * ratio).to_numpy().ravel(),
            "volume": raw["volume"].reindex_like(close).to_numpy().ravel(),
            "amount": raw["amount"].reindex_like(close).to_numpy().ravel(),
            "turnover": raw["turnover"].reindex_like(close).to_numpy().ravel(),
            "float_mv": (
                panel.market_cap.reindex_like(close).to_numpy().ravel()
                if panel.market_cap is not None
                else np.full(close.shape[0] * close.shape[1], np.nan)
            ),
            "total_mv": (
                panel.market_cap.reindex_like(close).to_numpy().ravel()
                if panel.market_cap is not None
                else np.full(close.shape[0] * close.shape[1], np.nan)
            ),
        }
    )
    bench_col = "bench_csi300"
    vol_col = "bench_vol_csi300"
    if factor_id.endswith("_sh"):
        bench_col = "bench_sh"
        vol_col = "bench_vol_sh"
    if bench_col in raw and not raw[bench_col].empty:
        bench = raw[bench_col].reindex(close.index).to_numpy()
        frame["benchmark_close"] = np.tile(bench, close.shape[1])
        if vol_col in raw and not raw[vol_col].empty:
            bvol = raw[vol_col].reindex(close.index).ffill().to_numpy()
            frame["benchmark_volume"] = np.tile(bvol, close.shape[1])
        else:
            frame["benchmark_volume"] = 1.0
    # 注意：必须传 .to_numpy()，否则 pd.Series(Series, index=...) 会按索引对齐，
    # 结果是整列 NaN（踩过一次）。
    wide = frame.groupby("symbol", group_keys=False).apply(
        lambda g: pd.Series(fn(g.reset_index(drop=True)).to_numpy(), index=g["ts"].values),
        include_groups=False,
    )
    # apply 返回的是 symbol × ts，转成 ts × symbol 并对齐到 close 面板
    packed = wide.T
    packed.index = pd.to_datetime(packed.index)
    out = packed.reindex(index=close.index, columns=close.columns)
    if getattr(panel, "eval_start", None) is not None:
        out = out.loc[out.index >= panel.eval_start]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="price", choices=("price", "financial"))
    parser.add_argument("--pool", type=int, default=200)
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--only", default=None, help="只跑某个类别，如 momentum")
    parser.add_argument("--render", action="store_true", help="同时输出 HTML 报告")
    parser.add_argument("--full", action="store_true", help="输出完整报告（中性化/成本/容量/卖出端/增量性）")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    end = args.end or now_cn().date().isoformat()
    panel = load_panel(args.pool_date, args.pool, args.start, end)
    pool = list(panel.close.columns)
    raw = _load_raw(pool, panel.pool.get("data_start", args.start), end)

    panel_cache: dict[str, pd.DataFrame] = {}

    def get_factor(fid: str) -> pd.DataFrame:
        if fid in panel_cache:
            return panel_cache[fid]
        if fid.split(".", 1)[0] in ("quality", "value"):
            frame = _financial_panel(fid, panel.close, args.start, end)
        else:
            frame = _factor_panel(fid, panel, raw)
        panel_cache[fid] = frame
        return frame

    from factors.ops.price_factors import ALL_NAMES

    financial = args.source == "financial"
    if financial:
        from factor_engine.fin_ext import FIN_EXT_SPECS
        from research.report.runner import _query as _q

        # 财务因子只在披露日更新：2026-10-29 是单日全量快照，历史日期约 193 只。
        # 取历史覆盖最高的 300 只作为财务样本，保证横截面 >=30 只且每天可比；
        # 不再用 5000+ 只（超大 IN 查询会打满 QuestDB，且与价量池不可比）。
        rows = _q(
            "select code, count() c from factor_value_daily "
            f"where factor_id='quality.roe_ttm' and ts between '{args.start}' and '{end}' "
            "and ts < '2026-10-29' group by code order by c desc limit 300"
        )
        fin_codes = [str(c) for c in rows["code"].tolist()] if not rows.empty else []
        universe_rows = _q(
            "select count_distinct(code) c from factor_value_daily "
            f"where factor_id='quality.roe_ttm' and ts between '{args.start}' and '{end}' "
            "and ts < '2026-10-29'"
        )
        fin_universe = int(universe_rows["c"].iloc[0]) if not universe_rows.empty else len(fin_codes)
        logger.info("财务因子样本 %s 只（有财务数据 %s 只，按历史覆盖排序取前 300）", len(fin_codes), fin_universe)
        fin_close, fin_open = _load_price_panel(fin_codes, args.start, end)
        if not fin_close.empty:
            # 2019 起算时，部分股票只有 2024 年后才有财务/行情；保留它们会把
            # 覆盖率稀释到 70% 以下并误触发「覆盖率不足」。用起始段覆盖率
            # （前 250 个交易日）筛选，保证整段是可比的同一批股票。
            early = fin_close.iloc[:250].notna().mean()
            keep = early[early >= 0.5].index
            if len(keep) >= 40:
                fin_close = fin_close.loc[:, keep]
                fin_open = fin_open.reindex(index=fin_close.index, columns=keep)
                fin_codes = list(keep)
            panel.close = fin_close
            panel.open = fin_open

    ids = [] if args.source == "financial" else [i for i in ALL_NAMES if not i.endswith(".nl_mv")]
    if financial:
        from factor_engine.fin_ext import FIN_EXT_SPECS

        ids += [spec.factor_id for spec in FIN_EXT_SPECS]
    if args.only:
        ids = [i for i in ids if i.split(".", 1)[0] == args.only]
    if args.limit:
        ids = ids[: args.limit]

    run_id = f"{now_cn().strftime('%Y%m%dT%H%M')}_{hashlib.sha1(f'{args.source}|{args.pool}|{args.start}'.encode()).hexdigest()[:6]}"
    logger.info("评分卡批次 %s：%s 个因子，池 %s 只", run_id, len(ids), len(pool))

    started = time.time()
    results = []
    for idx, factor_id in enumerate(ids, 1):
        try:
            panel_factor = get_factor(factor_id)
            if panel_factor.empty:
                logger.warning("%s 无物化数据（先跑 backfill_factors.py）", factor_id)
                continue
            peers = None
            if args.full:
                peers = {}
                for pid in peers_for(factor_id):
                    try:
                        peers[pid] = get_factor(pid)
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 计算失败：%s", factor_id, exc)
            continue
        definition = definitions.describe(factor_id)
        direction = definition.direction
        try:
            report = metrics.build_report(
                panel_factor,
                panel.close,
                panel.open,
                factor_id=factor_id,
                category=factor_id.split(".", 1)[0],
                direction=direction,
                direction_source=definition.direction_source,
                universe=(f"财务样本 {len(fin_codes)}" if financial else f"点位池 {args.pool}"),
                regime=panel.regime,
                market_cap=panel.market_cap if not financial else None,
                industry=panel.industry if not financial else None,
                definition=definition.as_dict(),
                pool=(
                    {
                        **panel.pool,
                        "kind": "financial",
                        "selection": "coverage",
                        "requested": 300,
                        "candidates": int(fin_universe),
                        "board_passed": int(min(300, fin_universe)),
                        "selected": int(len(fin_codes)),
                        "date": args.start,
                        "rule": "财务历史覆盖度前 300 只（按 roe_ttm 历史覆盖排序）",
                        "boards": "全市场有财务数据的股票（按覆盖度筛选）",
                        "survivorship_note": (
                            "财务池按历史覆盖度选取，退市股若没有财务数据会被整体排除，"
                            "收益与 IC 存在向上的幸存者偏差；覆盖度又与规模/上市年限相关，"
                            "因此财务族结论只能读作「大市值、老公司」子样本的条件结论。"
                        ),
                        "funnel": [
                            {"step": "区间内出现过财务数据的股票", "n": int(fin_universe), "note": "全库财务覆盖"},
                            {"step": "按覆盖度排序取前 300", "n": int(min(300, fin_universe)), "note": "覆盖度≈规模/上市年限"},
                            {"step": "起始段（前 250 日）有行情", "n": int(len(fin_codes)), "note": "剔除仅后期有数据的股票"},
                            {"step": "进入报告", "n": int(len(fin_codes)), "note": "最终样本"},
                        ],
                        "pit": definition.pit,
                    }
                    if financial else panel.pool
                ),
                freshness=panel.freshness,
                amount=panel.amount if not financial else None,
                peers=peers,
                full=args.full,
            )
            score = rating_mod.rate(report)
            scorecard.save(report, run_id=run_id, category=factor_id.split(".", 1)[0], horizon=5, score=score)
            results.append(
                {
                    "factor_id": factor_id,
                    "grade": score["grade"],
                    "score": score["score"],
                    "time": score["time_score"],
                    "space": score["space_score"],
                    "ic": float(report.ic[5].mean()) if 5 in report.ic else float("nan"),
                    "mono": float(report.monotonicity),
                }
            )
            if args.render:
                meta = {
                    "generated_at": now_cn().strftime("%Y-%m-%d %H:%M"),
                    "run_id": run_id,
                    "data_version": f"ohlcv_1d:{panel.close.index.max().date()}",
                }
                html = render.render(report, score, meta)
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                (OUT_DIR / f"{factor_id.replace('.', '_')}.html").write_text(html, encoding="utf-8")
            # JSON 与 HTML 同 run_id 落盘：审计/复现只读 JSON，不再解析 HTML
            meta = {
                "generated_at": now_cn().strftime("%Y-%m-%d %H:%M"),
                "run_id": run_id,
                "data_version": f"ohlcv_1d:{panel.close.index.max().date()}",
            }
            payload = build_payload(report, score, meta, definition.as_dict(), panel)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / f"{factor_id.replace('.', '_')}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            results[-1].update({
                "checks": payload.get("checks_status"),
                "attribution": (payload.get("attribution") or {}).get("label"),
                "spread_gross": (report.spread_gross or {}).get("mean", float("nan")),
                "spread_net": (report.spread_net or {}).get("mean", float("nan")),
                "family": diag.family_of(factor_id),
            })
            logger.info(
                "[%s/%s] %-38s %s %5.1f (IC %+.3f)",
                idx, len(ids), factor_id, score["grade"], score["score"],
                results[-1]["ic"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 评级失败：%s", factor_id, exc)
        finally:
            gc.collect()

    elapsed = time.time() - started
    if not results:
        logger.warning("没有因子成功评级")
        return 1
    summary = pd.DataFrame(results).sort_values("score", ascending=False)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_DIR / f"scorecard_{run_id}.csv", index=False, encoding="utf-8-sig")
    logger.info("完成 %s 个，耗时 %.0fs，结果写入 %s", len(results), elapsed, OUT_DIR)
    if not summary.empty:
        print(summary.head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
