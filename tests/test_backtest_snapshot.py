"""数据快照哈希：必须覆盖所有影响结果的列。"""

from __future__ import annotations

import pandas as pd

from backtest.infrastructure.data.snapshot import make_snapshot


def _frame(*, open_shift: float = 0.0, amount: float = 10_000.0) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=10)
    rows = []
    for i, dt in enumerate(dates):
        close = 10.0 + i
        rows.append(
            {
                "symbol": "AAA",
                "date": dt,
                "open": close * (1.0 + open_shift),
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000,
                "amount": amount,
                "adj_factor": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_snapshot_hash_covers_open_high_low():
    """只有 open 变化也必须换 hash，否则 run_id 会与结果不一致。"""
    base = make_snapshot("memory", _frame())
    shifted = make_snapshot("memory", _frame(open_shift=0.01))
    assert base.hash != shifted.hash


def test_snapshot_hash_covers_amount():
    base = make_snapshot("memory", _frame(amount=10_000.0))
    other = make_snapshot("memory", _frame(amount=20_000.0))
    assert base.hash != other.hash


def test_snapshot_hash_is_order_insensitive():
    frame = _frame()
    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    assert make_snapshot("memory", frame).hash == make_snapshot("memory", shuffled).hash
