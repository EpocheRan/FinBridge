import pytest

from backtest.application.loader import list_strategies, load_strategy
from backtest.domain.errors import StrategyLoadError


def test_load_strategy_class_and_params():
    loaded = load_strategy("strategies/models/atr_trend.py", {"n": 10})
    assert loaded.strategy.name == "atr_trend"
    assert loaded.strategy.mode == "single"
    assert loaded.strategy.params["n"] == 10
    assert [item.name for item in loaded.strategy.factors()] == ["atr10", "ma20"]
    assert len(loaded.source_hash) == 64


def test_list_strategies():
    items = list_strategies("strategies/models")
    names = {item.name for item in items}
    assert {"atr_trend", "rank_mom20"} <= names


def test_strategy_must_be_class_or_factory(tmp_path):
    path = tmp_path / "bad_strategy.py"
    path.write_text(
        "from strategies.models.atr_trend import AtrTrend\nSTRATEGY = AtrTrend()\n",
        encoding="utf-8",
    )
    with pytest.raises(StrategyLoadError, match="不能是实例"):
        load_strategy(path)


def test_strategy_must_define_strategy(tmp_path):
    path = tmp_path / "empty_strategy.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(StrategyLoadError, match="缺少模块级 STRATEGY"):
        load_strategy(path)
