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


def test_type_error_inside_init_is_wrapped(tmp_path):
    """__init__ 内部的 TypeError 不能被当成“签名不兼容”吞掉。"""
    path = tmp_path / "boom.py"
    path.write_text(
        "from strategies.base import SingleSymbolStrategy\n"
        "class Boom(SingleSymbolStrategy):\n"
        "    name = 'boom'\n"
        "    symbols = ('600519',)\n"
        "    def __init__(self, params=None):\n"
        "        super().__init__(params)\n"
        "        raise TypeError('boom inside init')\n"
        "    def factors(self):\n"
        "        return []\n"
        "STRATEGY = Boom\n",
        encoding="utf-8",
    )
    with pytest.raises(StrategyLoadError) as excinfo:
        load_strategy(path)
    message = str(excinfo.value)
    assert "策略实例化失败" in message
    assert "TypeError" in message
    assert "boom inside init" in message


def test_factory_without_params_still_applies_cli_params(tmp_path):
    """不支持 params 的工厂：参数仍要合并并重新校验，不能静默丢弃。"""
    path = tmp_path / "factory.py"
    path.write_text(
        "from strategies.models.atr_trend import AtrTrend\n"
        "def create_strategy():\n"
        "    return AtrTrend()\n"
        "STRATEGY = create_strategy\n",
        encoding="utf-8",
    )
    loaded = load_strategy(path, {"n": 7})
    assert loaded.strategy.params["n"] == 7
    assert [item.name for item in loaded.strategy.factors()] == ["atr7", "ma20"]
