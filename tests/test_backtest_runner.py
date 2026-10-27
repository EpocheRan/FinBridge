import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pybroker")

from backtest.bootstrap import build_runner
from backtest.config import BacktestSettings
from backtest.domain.errors import BacktestError, ConfigError
from backtest.domain.models import SecurityStatus
from backtest.infrastructure.data.memory import MemoryDataSource
from backtest.infrastructure.engine.pybroker_engine import PyBrokerEngine
from backtest.infrastructure.output.console import ConsoleOutput

START = "2024-03-01"
END = "2025-01-31"


def _frame(days: int = 320, open_shift: float = -0.005) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    for i, dt in enumerate(dates):
        close = 100 + i * 0.05 + 5 * np.sin(i / 10)
        rows.append(
            {
                "symbol": "600519",
                "date": dt,
                "open": close * (1.0 + open_shift),
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 100_000,
                "adj_factor": 1.0,
                "hfq_close": close,
            }
        )
    return pd.DataFrame(rows)


_CROSS_SYMBOLS = ("600519", "000001", "300750", "600036", "000858", "601318")


def _cross_frame(days: int = 320) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    profiles = {
        "600519": (100.0, 0.30, 0.0),
        "000001": (15.0, 0.10, 1.7),
        "300750": (60.0, 0.22, 3.1),
        "600036": (30.0, 0.08, 4.6),
        "000858": (80.0, 0.16, 5.9),
        "601318": (45.0, 0.05, 2.4),
    }
    rows = []
    for symbol in _CROSS_SYMBOLS:
        base, trend, phase = profiles[symbol]
        for i, dt in enumerate(dates):
            close = base + i * trend + 1.5 * np.sin((i + phase) / 6.0)
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": close * 0.998,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 100_000,
                    "adj_factor": 1.0,
                    "hfq_close": close,
                }
            )
    return pd.DataFrame(rows)


def _runner(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    security_status: SecurityStatus | None = None,
    **overrides,
):
    settings = BacktestSettings(
        initial_cash=1_000_000,
        outputs=("console",),
        warmup_min_bars=30,
        data="memory",
        **overrides,
    )
    return build_runner(
        settings=settings,
        data_source=MemoryDataSource(
            frame, benchmark=benchmark, security_status=security_status
        ),
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


def test_runner_optimize_grid_returns_best_params():
    runner = _runner(_frame())
    result = runner.optimize(
        "strategies/models/atr_trend.py",
        start=START,
        end=END,
        grid={"k": [1.0, 2.0, 3.0]},
        metric="sharpe",
    )
    assert len(result.trials) == 3
    assert all(trial.error is None for trial in result.trials)
    assert result.best_params is not None
    assert result.best_params["k"] in {1.0, 2.0, 3.0}
    assert result.trials_frame().shape[0] == 3


def test_runner_walkforward_returns_folds():
    runner = _runner(_frame())
    result = runner.walkforward(
        "strategies/models/atr_trend.py",
        start=START,
        end=END,
        grid={"k": [1.0, 2.0]},
        metric="sharpe",
        windows=2,
        train_size=0.6,
        lookahead=1,
    )
    assert len(result.folds) == 2
    assert result.folds_frame().shape[0] == 2
    assert all(fold.test_start <= fold.test_end for fold in result.folds)
    assert result.best_params is not None


def test_run_id_includes_security_status():
    base = _runner(_frame()).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    with_status = _runner(
        _frame(),
        security_status=SecurityStatus(
            current_st={"600519": True}, approximation=True
        ),
    ).run("strategies/models/atr_trend.py", start=START, end=END)
    assert base.run_id != with_status.run_id
    assert with_status.meta["st_approximation"] is True


def test_runner_without_benchmark_marks_not_used():
    result = _runner(_frame()).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    assert result.meta["benchmark_used"] is False
    assert "beta" not in result.metrics


def test_runner_applies_approximate_corporate_actions():
    frame = _frame()
    event_date = frame["date"].iloc[100]
    frame.loc[frame["date"] == event_date, "adj_factor"] = 1.1
    result = _runner(frame, corporate_actions="approx").run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    assert result.meta["corporate_action_approximation"] is True
    assert result.corporate_actions is not None
    assert not result.corporate_actions.empty


def test_runner_computes_benchmark_alpha_beta():
    frame = _frame()
    dates = pd.bdate_range("2024-01-02", periods=320)
    benchmark = pd.DataFrame(
        {
            "symbol": "000300",
            "date": dates,
            "close": 100.0 + np.arange(len(dates)) * 0.05 + np.sin(np.arange(len(dates)) / 5.0),
        }
    )
    result = _runner(frame, benchmark=benchmark).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    assert result.meta["benchmark_used"] is True
    assert result.meta["benchmark_points"] > 0
    assert "beta" in result.metrics
    assert "alpha_annualized_pct" in result.metrics
    assert "excess_return_pct" in result.metrics


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


def test_cross_section_runs_with_candidate_set():
    """横截面：全 universe 排名 → 候选集 → PyBroker 执行。"""
    frame = _cross_frame()
    result = _runner(frame).run(
        "strategies/models/rank_mom20.py", start=START, end=END
    )
    assert result.spec.mode == "cross"
    assert result.meta["mode"] == "cross"
    assert 0 < result.meta["candidate_count"] <= len(_CROSS_SYMBOLS)
    assert result.meta["universe_size"] == len(_CROSS_SYMBOLS)
    assert not result.selections.empty
    assert set(result.selections["symbol"]) <= set(_CROSS_SYMBOLS)

    # 每个调仓日选 top_n 只，权重和不超过 max_gross_exposure
    grouped = result.selections.groupby("date")
    assert (grouped.size() == 3).all()
    assert (grouped["weight"].sum() <= 0.98 + 1e-9).all()

    # 只交易候选集，且账本现金不为负
    assert not result.orders.empty
    assert set(result.orders["symbol"]) <= set(result.selections["symbol"])
    assert (result.equity["cash"] >= -1e-6).all()
    assert result.meta["ledger_reconciliation"]["points"] > 0
    assert len(result.run_id) == 16
    # 所有买单都应在调仓日（created）产生；非调仓日只允许 buffer 卖出。
    rebalance_dates = set(pd.to_datetime(result.selections["date"]).dt.normalize())
    created = pd.to_datetime(result.orders["created"]).dt.normalize()
    buys = result.orders[result.orders["type"] == "buy"]
    assert set(pd.to_datetime(buys["created"]).dt.normalize()) <= rebalance_dates
    outside = result.orders[~created.isin(rebalance_dates)]
    assert (outside["type"] == "sell").all()


def test_fill_close_uses_fill_bar_close():
    frame = _frame()
    result = _runner(frame, fill="close").run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    order = result.orders.iloc[0]
    day = pd.Timestamp(order["date"]).normalize()
    bar = frame[frame["date"] == day].iloc[0]
    # PyBroker 默认 round_fill_price=True，成交价会四舍五入到分。
    assert float(order["fill_price"]) == pytest.approx(
        round(float(bar["close"]), 2), abs=1e-9
    )
    assert result.meta["fill"] == "close"
    assert result.meta["fill_price_type"] == "close"


def test_max_gross_exposure_caps_buy():
    frame = _frame()
    result = _runner(frame, max_gross_exposure=0.5).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    order = result.orders.iloc[0]
    cost = float(order["shares"]) * float(order["fill_price"])
    # 允许一个整手的取整余量；核心是“不超过 50% 仓位”。
    assert cost <= 0.5 * 1_000_000 + float(order["fill_price"]) * 100


def test_run_id_changes_when_open_changes():
    """open 变化必须改变数据快照与 run_id（防止快照只哈希 close）。"""
    first = _runner(_frame(open_shift=-0.005)).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    second = _runner(_frame(open_shift=0.0)).run(
        "strategies/models/atr_trend.py", start=START, end=END
    )
    assert first.run_id != second.run_id


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"corporate_actions": "exact"}, "corporate_actions"),
        ({"allow_short": True}, "做空"),
        ({"factor_source": "stored"}, "stored"),
        ({"fill": "tomorrow"}, "fill"),
        ({"max_gross_exposure": 1.5}, "max_gross_exposure"),
    ],
)
def test_unsupported_or_invalid_settings_fail_fast(overrides, match):
    runner = _runner(_frame(), **overrides)
    with pytest.raises(ConfigError, match=match):
        runner.run("strategies/models/atr_trend.py", start=START, end=END)


def test_cross_section_requires_explicit_universe(tmp_path):
    path = tmp_path / "all_a.py"
    path.write_text(
        "from strategies import CrossSectionStrategy, Universe\n"
        "from factors import F\n"
        "import pandas as pd\n"
        "class AllA(CrossSectionStrategy):\n"
        "    name = 'all_a'\n"
        "    universe = Universe.all_a()\n"
        "    top_n = 2\n"
        "    def factors(self):\n"
        "        return [F.mom(5)]\n"
        "    def rank(self, panel, dt):\n"
        "        return pd.Series(dtype=float)\n"
        "STRATEGY = AllA\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="explicit"):
        _runner(_cross_frame()).run(path, start=START, end=END)


def test_cross_buffer_exit_on_non_rebalance_day(tmp_path):
    path = tmp_path / "rotate.py"
    path.write_text(
        "from strategies import CrossSectionStrategy, Universe, Rebalance\n"
        "from factors import F\n"
        "import pandas as pd\n"
        "class Rotate(CrossSectionStrategy):\n"
        "    name = 'rotate'\n"
        "    universe = Universe.explicit(('600519','000001','300750','600036'))\n"
        "    top_n = 1\n"
        "    buffer = 1\n"
        "    rebalance = Rebalance.WEEKLY\n"
        "    def factors(self):\n"
        "        return [F.mom(1)]\n"
        "    def rank(self, panel, dt):\n"
        "        if pd.Timestamp(dt) < pd.Timestamp('2024-02-01'):\n"
        "            return pd.Series({'600519':4,'000001':3,'300750':2,'600036':1})\n"
        "        return pd.Series({'300750':4,'000001':3,'600036':2,'600519':1})\n"
        "STRATEGY = Rotate\n",
        encoding="utf-8",
    )
    result = _runner(_cross_frame()).run(path, start="2024-01-02", end="2024-04-30")

    sells = result.orders[
        (result.orders["type"] == "sell") & (result.orders["symbol"] == "600519")
    ]
    assert not sells.empty
    rebalance_dates = set(pd.to_datetime(result.selections["date"]).dt.normalize())
    created = pd.to_datetime(sells["created"]).dt.normalize()
    # 排名掉到 top_n+buffer 之外 → 非调仓日也要卖出
    assert any(moment not in rebalance_dates for moment in created)
    assert (created >= pd.Timestamp("2024-02-01")).any()

    buys = result.orders[
        (result.orders["type"] == "buy") & (result.orders["symbol"] == "600519")
    ]
    assert (pd.to_datetime(buys["created"]) < pd.Timestamp("2024-02-01")).all()


def _limit_up_frame(days: int = 100) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=days)
    rows = []
    aaa_close = 10.0
    for i, dt in enumerate(dates):
        if i:
            aaa_close = round(aaa_close * 1.1, 2)
        rows.append(
            {
                "symbol": "AAA",
                "date": dt,
                "open": aaa_close,
                "high": aaa_close,
                "low": aaa_close,
                "close": aaa_close,
                "volume": 100_000,
                "adj_factor": 1.0,
                "hfq_close": aaa_close,
            }
        )
        rows.append(
            {
                "symbol": "BBB",
                "date": dt,
                "open": 20.0,
                "high": 20.0,
                "low": 20.0,
                "close": 20.0,
                "volume": 100_000,
                "adj_factor": 1.0,
                "hfq_close": 20.0,
            }
        )
    return pd.DataFrame(rows)


def test_cross_limit_up_blocks_buy(tmp_path):
    path = tmp_path / "limit_up.py"
    path.write_text(
        "from strategies import CrossSectionStrategy, Universe, Rebalance\n"
        "from factors import F\n"
        "import pandas as pd\n"
        "class LimitUp(CrossSectionStrategy):\n"
        "    name = 'limit_up'\n"
        "    universe = Universe.explicit(('AAA','BBB'))\n"
        "    top_n = 1\n"
        "    buffer = 0\n"
        "    rebalance = Rebalance.WEEKLY\n"
        "    def factors(self):\n"
        "        return [F.mom(1)]\n"
        "    def rank(self, panel, dt):\n"
        "        return pd.Series({'AAA':2,'BBB':1})\n"
        "STRATEGY = LimitUp\n",
        encoding="utf-8",
    )
    result = _runner(_limit_up_frame()).run(path, start="2024-03-01", end="2024-05-31")

    rejected = result.execution_report.rejected
    assert not rejected.empty
    assert (rejected["reason"] == "limit_up").any()
    buys = result.orders[
        (result.orders["type"] == "buy") & (result.orders["symbol"] == "AAA")
    ]
    assert buys.empty
