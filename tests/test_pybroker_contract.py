"""PyBroker 2.0.x API 契约测试。

这些用例不是业务测试，而是把 ``docs/design/backtest_architecture.md`` §9.1/§9.2 依赖的
PyBroker 行为钉死：**失败意味着设计假设错了，应先改设计，而不是改测试**。

未安装 pybroker 时整模块跳过（与 tests/test_backtest_data_source.py 一致）。
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

pybroker = pytest.importorskip("pybroker")

from pybroker import ExecContext, Strategy, StrategyConfig  # noqa: E402
from pybroker.common import PositionMode, PriceType  # noqa: E402

# pybroker.hyperparam 是进程级全局注册；本模块用完 ``period`` 后，
# 后续用例的 strategy 会看到"已注册但未被引用"的告警。与设计无关，静音即可。
pytestmark = pytest.mark.filterwarnings(
    "ignore:Hyperparam 'period' is registered but not reachable:UserWarning"
)

START = "2024-01-02"
END = "2024-08-30"


def _bars(symbols: tuple[str, ...] = ("AAA",), bars: int = 40) -> pd.DataFrame:
    """构造 open / high / low / close 两两不等的确定性日线。

    close = 12 + i，open = 10 + i，middle = (high + low) / 2 = 11 + i，
    这样"中间价成交"与"开盘价成交"可以被区分开。
    """
    dates = pd.bdate_range(START, periods=bars)
    rows = []
    for symbol in symbols:
        for i, dt in enumerate(dates):
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": 10.0 + i,
                    "high": 14.0 + i,
                    "low": 8.0 + i,
                    "close": 12.0 + i,
                    "volume": 10_000,
                }
            )
    return pd.DataFrame(rows)


def _run(
    exec_fn,
    *,
    symbols: tuple[str, ...] = ("AAA",),
    bars: int = 40,
    config: StrategyConfig | None = None,
    fill: PriceType | None = None,
):
    frame = _bars(symbols, bars)
    strategy = Strategy(
        frame,
        START,
        END,
        config=config or StrategyConfig(initial_cash=100_000),
    )

    def fn(ctx: ExecContext) -> None:
        exec_fn(ctx)
        # fill price 只能和订单同 bar 设置，否则 to_result() 会抛 ValueError。
        if fill is not None:
            if ctx.buy_shares is not None:
                ctx.buy_fill_price = fill
            if ctx.sell_shares is not None:
                ctx.sell_fill_price = fill

    strategy.add_execution(fn, list(symbols))
    return strategy.backtest(), frame


def _buy_on_bar(bar: int):
    def fn(ctx: ExecContext) -> None:
        if ctx.bars == bar and not ctx.long_pos():
            ctx.buy_shares = 100

    return fn


def _first_fill(result, frame: pd.DataFrame) -> tuple[float, float, float]:
    """返回 (成交价, 成交日 open, 成交日 middle=(high+low)/2)。"""
    assert not result.orders.empty, "回测没有产生订单"
    order = result.orders.iloc[0]
    day = pd.Timestamp(order["date"])
    symbol = order["symbol"]
    row = frame[(frame["symbol"] == symbol) & (frame["date"] == day)].iloc[0]
    middle = (float(row["high"]) + float(row["low"])) / 2.0
    return float(order["fill_price"]), float(row["open"]), middle


# ---------------------------------------------------------------- #1 / #2 成交价


def test_default_fill_price_is_middle():
    """不设 buy_fill_price 时成交价是中间价（默认 PriceType.MIDDLE）。"""
    result, frame = _run(_buy_on_bar(5))
    fill, open_price, middle = _first_fill(result, frame)
    assert fill == pytest.approx(middle, abs=1e-9)
    assert fill != pytest.approx(open_price, abs=1e-9)


def test_fill_price_open_matches_next_bar_open():
    """显式 PriceType.OPEN 时，成交价等于信号次日开盘价。"""
    result, frame = _run(_buy_on_bar(5), fill=PriceType.OPEN)
    fill, open_price, _ = _first_fill(result, frame)
    assert fill == pytest.approx(open_price, abs=1e-9)


# ---------------------------------------------------------------- #3 / #4 止损


def test_stop_without_order_raises():
    """只设止损、不下单：to_result() 抛 ValueError（止损只能随单设置）。"""

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == 5:
            ctx.stop_loss_pct = 5

    with pytest.raises(ValueError):
        _run(fn)


def test_stop_with_order_is_accepted():
    """止损与买入单同一根 bar 设置时，订单与止损都能生效。"""

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == 5 and not ctx.long_pos():
            ctx.buy_shares = 100
            ctx.stop_loss_pct = 5

    result, _ = _run(fn)
    assert not result.orders.empty


# ---------------------------------------------------------------- #5 股数不整手


def test_calc_target_shares_does_not_round_to_lot():
    """set_target_shares / calc_target_shares 不做 100 股整手。"""
    captured: dict[str, int] = {}

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == 5 and not ctx.long_pos():
            shares = ctx.calc_target_shares(0.1, price=13.37, cash=100_000)
            captured["shares"] = int(shares)
            ctx.buy_shares = int(shares)

    result, _ = _run(fn)
    assert captured["shares"] == 747  # 10000 / 13.37 = 747.94，向下取整
    assert captured["shares"] % 100 != 0
    assert not result.orders.empty


# ---------------------------------------------------------------- #6 after_exec


def test_after_exec_receives_all_symbols_and_can_order():
    """set_after_exec 收到全部 symbol 的 context，并且可以下单。"""
    frame = _bars(("AAA", "BBB"), 40)
    strategy = Strategy(
        frame, START, END, config=StrategyConfig(initial_cash=100_000)
    )
    strategy.add_execution(None, ["AAA", "BBB"])
    seen: dict[str, tuple[str, ...]] = {}

    def rebalance(ctxs: dict[str, ExecContext]) -> None:
        seen["symbols"] = tuple(sorted(ctxs))
        if next(iter(ctxs.values())).bars == 5:
            for ctx in ctxs.values():
                ctx.buy_fill_price = PriceType.OPEN
                ctx.buy_shares = 100

    strategy.set_after_exec(rebalance)
    result = strategy.backtest()

    assert seen["symbols"] == ("AAA", "BBB")
    assert sorted(result.orders["symbol"].unique()) == ["AAA", "BBB"]


# ---------------------------------------------------------------- #7 optimize


def test_optimize_windows_runs():
    """optimize(windows=3) 能产出 3 个 walkforward 窗口。"""
    period = pybroker.hyperparam("period", default=5, low=3, high=5, step=2)
    frame = _bars(("AAA",), 120)
    strategy = Strategy(
        frame, START, END, config=StrategyConfig(initial_cash=100_000)
    )

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == ctx.hyperparam("period") and not ctx.long_pos():
            ctx.buy_shares = 100

    strategy.add_execution(fn, "AAA", hyperparams=[period])
    opt = strategy.optimize(
        lambda result: result.metrics.total_return_pct,
        sampler="grid",
        windows=3,
    )
    assert len(opt.windows) == 3


# ---------------------------------------------------------------- #8 LONG_ONLY


def test_long_only_rejects_naked_short():
    """PositionMode.LONG_ONLY 下 sell_shares 不会开空头。"""
    config = StrategyConfig(
        initial_cash=100_000, position_mode=PositionMode.LONG_ONLY
    )

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == 5:
            ctx.sell_shares = 100

    result, _ = _run(fn, config=config)
    if not result.orders.empty:
        intents = set(result.orders["intent"].astype(str))
        assert "sell_to_open" not in intents


# ---------------------------------------------------------------- #9 费用方向


def test_custom_fee_sees_buy_and_sell_direction():
    """FeeInfo.order_type 可区分 buy/sell，能按方向收印花税。"""
    from pybroker.common import FeeInfo

    seen: list[str] = []

    def fee_fn(info: FeeInfo) -> Decimal:
        seen.append(str(info.order_type))
        return Decimal("0")

    config = StrategyConfig(initial_cash=100_000, fee_mode=fee_fn)

    def fn(ctx: ExecContext) -> None:
        if ctx.bars == 5 and not ctx.long_pos():
            ctx.buy_shares = 100
        elif ctx.bars == 8 and ctx.long_pos():
            ctx.sell_shares = 100

    result, _ = _run(fn, config=config)
    assert "buy" in seen
    assert "sell" in seen
    assert not result.orders.empty
