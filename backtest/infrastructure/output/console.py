"""Console 输出。"""

from __future__ import annotations

from backtest.domain.models import BacktestResult, OutputArtifact


class ConsoleOutput:
    name = "console"

    def emit(self, result: BacktestResult) -> list[OutputArtifact]:
        print(f"run_id: {result.run_id}")
        print(f"strategy: {result.spec.strategy_name} ({result.spec.mode})")
        print(f"period: {result.spec.start} .. {result.spec.end}  freq={result.spec.freq}")
        print("metrics:")
        for key, value in sorted(result.metrics.items()):
            print(f"  {key}: {value:.6f}" if isinstance(value, float) else f"  {key}: {value}")
        if not result.equity.empty:
            print("equity head:")
            print(result.equity.head(3).to_string(index=False))
            print("equity tail:")
            print(result.equity.tail(3).to_string(index=False))
        print(f"orders: {len(result.orders)}  trades: {len(result.trades)}")
        rejected = result.execution_report.rejected
        unfilled = result.execution_report.unfilled
        print(f"rejected: {len(rejected)}  unfilled: {len(unfilled)}")
        if result.meta.get("ledger_reconciliation"):
            print(f"ledger reconciliation: {result.meta['ledger_reconciliation']}")
        return [OutputArtifact(kind="console", rows=len(result.equity))]
