import json
from datetime import date

import pandas as pd

from backtest.domain.models import BacktestResult, ExecutionReport, RunSpec
from backtest.infrastructure.output.csv import CsvOutput


def _result() -> BacktestResult:
    spec = RunSpec(
        strategy_name="demo",
        strategy_path="demo.py",
        mode="single",
        symbols=("600519",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        freq="d",
        initial_cash=100_000,
    )
    return BacktestResult(
        run_id="run123",
        spec=spec,
        metrics={"total_return_pct": 1.0},
        metrics_pybroker={"total_return_pct": 0.9},
        equity=pd.DataFrame(
            {
                "date": [pd.Timestamp("2024-01-02")],
                "cash": [100_000.0],
                "market_value": [0.0],
                "equity": [100_000.0],
                "drawdown": [0.0],
            }
        ),
        orders=pd.DataFrame(columns=["type", "symbol", "date", "shares", "fill_price", "fees"]),
        trades=pd.DataFrame(columns=["type", "symbol"]),
        selections=pd.DataFrame(columns=["date", "symbol"]),
        factor_meta={},
        execution_report=ExecutionReport(),
        meta={"metrics_source": "ledger"},
    )


def test_csv_output_writes_expected_files(tmp_path):
    output = CsvOutput(tmp_path)
    artifacts = output.emit(_result())
    run_dir = tmp_path / "run123"
    for name in (
        "metrics.json",
        "equity.csv",
        "orders.csv",
        "trades.csv",
        "selections.csv",
        "rejected.csv",
        "unfilled.csv",
        "corporate_actions.csv",
    ):
        assert (run_dir / name).exists(), name
    payload = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == "run123"
    assert payload["metrics"]["total_return_pct"] == 1.0
    assert len(artifacts) >= 8
