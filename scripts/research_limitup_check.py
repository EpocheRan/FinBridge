#!/usr/bin/env python
"""可成交性检验：剔除「T+1 开盘一字涨停」后，因子的 IC 与分层收益还剩多少。

背景
----
研究报告原先假设「只要出信号，就能按 T+1 开盘价买到」。但 A 股一字涨停时开盘即封死，
实盘买不进——动量/反转类因子的 Top 组恰恰最容易踩中这种票，导致研究层系统性高估。

本脚本对同一批因子跑两遍：
- 原始：全部样本；
- 可成交：剔除 t+1 开盘价 ≥ 涨停价的样本（涨停价按板块规则由 t 日收盘推出）。

用法::

    python scripts/research_limitup_check.py
    python scripts/research_limitup_check.py --factors momentum.ret@20,momentum.ret@60
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

from research.report import metrics  # noqa: E402
from research.report.runner import compute_factor, load_panel  # noqa: E402

logger = logging.getLogger("Scripts.LimitUpCheck")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factors", default="momentum.ret@20,momentum.ret@60,reversal.short@5,volatility.20d")
    parser.add_argument("--pool", type=int, default=500)
    parser.add_argument("--pool-date", default="2017-11-01")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--horizon", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    panel = load_panel(args.pool_date, args.pool, args.start, args.end or "2026-10-28")
    fwd = metrics.forward_returns(panel.close, panel.open, (args.horizon,))[args.horizon]
    mask = metrics.tradeable_mask(panel.close, panel.open)
    fwd_masked = fwd.where(mask)

    print("\n" + "=" * 110)
    print(f"可成交性检验（池 {args.pool} 只，h={args.horizon}，T+1 开盘买入）")
    print("=" * 110)
    print(f"全样本中「t+1 开盘即涨停/停牌」的占比：{(~mask).to_numpy().mean():.2%}")
    print("-" * 110)
    head = (f"{'因子':<22}{'IC原始':>9}{'IC可成交':>10}{'衰减':>8}"
            f"{'价差原始':>10}{'价差可成交':>12}{'衰减':>8}{'Top组涨停率':>12}")
    print(head)
    print("-" * 110)

    for factor_id in [f.strip() for f in args.factors.split(",") if f.strip()]:
        factor = compute_factor(factor_id, panel.close)
        ic_raw = metrics._rank_ic_frame(factor, fwd)
        ic_msk = metrics._rank_ic_frame(factor, fwd_masked)

        # 每日分组价差（Top - Bottom），不复合、不计成本，只比原始幅度
        spreads_raw, spreads_msk = [], []
        dates = factor.index
        for i in range(0, len(dates) - args.horizon - 1, args.horizon):
            dt = dates[i]
            row = factor.loc[dt].dropna()
            if len(row) < 50:
                continue
            q = pd.qcut(row.rank(method="first"), 5, labels=False)
            top, bot = row[q == 4].index, row[q == 0].index
            r_raw = fwd.loc[dt]
            if r_raw.reindex(top).notna().sum() > 3 and r_raw.reindex(bot).notna().sum() > 3:
                spreads_raw.append(r_raw.reindex(top).mean() - r_raw.reindex(bot).mean())
            ok = mask.loc[dt] if dt in mask.index else None
            if ok is not None:
                top_ok = [c for c in top if ok.get(c, False)]
                bot_ok = [c for c in bot if ok.get(c, False)]
                if len(top_ok) > 3 and len(bot_ok) > 3:
                    r = fwd_masked.loc[dt]
                    spreads_msk.append(r.reindex(top_ok).mean() - r.reindex(bot_ok).mean())

        top_rate = []
        for i in range(0, len(dates) - args.horizon - 1, args.horizon):
            dt = dates[i]
            row = factor.loc[dt].dropna()
            if len(row) < 50 or dt not in mask.index:
                continue
            q = pd.qcut(row.rank(method="first"), 5, labels=False)
            top = row[q == 4].index
            ok = mask.loc[dt].reindex(top)
            if len(ok):
                top_rate.append(float((~ok.fillna(False)).mean()))

        s_raw = float(np.nanmean(spreads_raw)) if spreads_raw else float("nan")
        s_msk = float(np.nanmean(spreads_msk)) if spreads_msk else float("nan")
        decay_ic = (ic_msk.mean() - ic_raw.mean()) / abs(ic_raw.mean()) if ic_raw.mean() else float("nan")
        decay_sp = (s_msk - s_raw) / abs(s_raw) if s_raw else float("nan")
        print(f"{factor_id:<22}{ic_raw.mean():>+9.3f}{ic_msk.mean():>+10.3f}{decay_ic:>8.0%}"
              f"{s_raw*100:>9.2f}%{s_msk*100:>11.2f}%{decay_sp:>8.0%}"
              f"{float(np.mean(top_rate)) if top_rate else float('nan'):>11.1%}")
    print("=" * 110)
    print("说明：IC 为逐日 Spearman；价差为「Top 组均值 - Bottom 组均值」的每期平均，未扣成本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
