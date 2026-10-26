import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pybroker")

from backtest.bootstrap import build_runner
from backtest.config import BacktestSettings
from backtest.domain.errors import BacktestError
from backtest.infrastructure.data.memory import MemoryDataSource
from backtest.infrastructure.engine.pybroker_engine import PyBrokerEngine
from backtest.infrastructure.output.console import ConsoleOutput


def _frame(days: int = 320) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for i, dt in enumerate(dates):
        close = 100 + i * 0.05 + 5 * np.sin(i / 10)
        rows.append(
            {
                "symbol": "600519",
                "date": dt,
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 100_000,
                "adj_factor": 1.0,
                "hfq_close": close,
            }
        )
    return pd.DataFrame(rows)


def _runner(frame: pd.DataFrame):
    settings = BacktestSettings(
        initial_cash=1_000_000,
        outputs=("console",),
        warmup_min_bars=30,
        data="memory",
    )
    return build_runner(
        settings=settings,
        data_source=MemoryDataSource(frame),
        engine=PyBrokerEngine(),
        outputs=[ConsoleOutput()],
    )


def test_runner_single_symbol_and_run_id_stability(capsys):
    frame = _frame()
    runner = _runner(frame)
    result = runner.run(
        "strategies/models/atr_trend.py",
        start="2024-03-01",
        end="2025-01-31",
        freq="d",
    )
    assert len(result.run_id) == 16
    assert result.metrics["total_return_pct"] != 0
    assert result.equity["date"].min() >= pd.Timestamp("2024-03-01")
    assert not result.orders.empty
    assert result.metrics_pybroker
    assert result.meta["metrics_source"] == "ledger"
    assert result.meta["ledger_reconciliation"]["points"] > 0

    again = _runner(frame).run(
        "strategies/models/atr_trend.py",
        start="2024-03-01",
        end="2025-01-31",
        freq="d",
    )
    assert again.run_id == result.run_id
    captured = capsys.readouterr()
    assert "run_id" in captured.out


def test_runner_run_id_changes_with_strategy_params():
    frame = _frame()
    runner = _runner(frame)
    first = runner.run(
        "strategies/models/atr_trend.py",
        start="2024-03-01",
        end="2025-01-31",
        params={"n": 14},
    )
    second = _runner(frame).run(
        "strategies/models/atr_trend.py",
        start="2024-03-01",
        end="2025-01-31",
        params={"n": 10},
    )
    assert first.run_id != second.run_id


def test_runner_rejects_cross_section_in_mvp():
    runner = _runner(_frame())
    with pytest.raises(BacktestError, match="单股"):
        runner.run(
            "strategies/models/rank_mom20.py",
            start="2024-03-01",
            end="2025-01-31",
        )
