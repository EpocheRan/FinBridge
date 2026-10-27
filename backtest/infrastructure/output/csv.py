"""CSV 输出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.domain.models import BacktestResult, OutputArtifact


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "to_payload"):
        return value.to_payload()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


class CsvOutput:
    name = "csv"

    def __init__(self, output_dir: str | Path = "output/backtest"):
        self.output_dir = Path(output_dir)

    def emit(self, result: BacktestResult) -> list[OutputArtifact]:
        run_dir = self.output_dir / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[OutputArtifact] = []

        metrics_payload = {
            "run_id": result.run_id,
            "metrics": _json_safe(result.metrics),
            "metrics_pybroker": _json_safe(result.metrics_pybroker),
            "meta": _json_safe(result.meta),
            "factor_meta": _json_safe(result.factor_meta),
        }
        metrics_path = run_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(metrics_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts.append(OutputArtifact(kind="file", path=metrics_path, rows=1))

        tables = {
            "equity": result.equity,
            "orders": result.orders,
            "trades": result.trades,
            "selections": result.selections,
            "rejected": result.execution_report.rejected,
            "unfilled": result.execution_report.unfilled,
            "corporate_actions": (
                result.corporate_actions
                if result.corporate_actions is not None
                else result.execution_report.corporate_actions
            ),
        }
        for name, frame in tables.items():
            path = run_dir / f"{name}.csv"
            if frame is None:
                frame = pd.DataFrame()
            frame.to_csv(path, index=False)
            artifacts.append(OutputArtifact(kind="file", path=path, rows=len(frame)))
        return artifacts
