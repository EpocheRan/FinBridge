"""配置解析：未知字段与非法取值的报错要可定位。"""

from __future__ import annotations

import pytest

from backtest.config import BacktestSettings
from backtest.domain.errors import ConfigError
from backtest.domain.models import ExecutionRules


def test_unknown_backtest_key_raises_config_error():
    with pytest.raises(ConfigError, match="未知字段"):
        BacktestSettings.from_dict({"unknown_key": 1})


def test_unknown_rules_key_raises_config_error():
    with pytest.raises(ConfigError, match="未知字段"):
        ExecutionRules.from_dict({"typo_rate": 0.1})


def test_valid_settings_parse_outputs_and_fill():
    settings = BacktestSettings.from_dict(
        {
            "fill": "close",
            "outputs": "console,csv",
            "max_gross_exposure": 0.9,
            "rules": {"lot_size": 100, "t_plus_1": False},
        }
    )
    assert settings.fill == "close"
    assert settings.outputs == ("console", "csv")
    assert settings.max_gross_exposure == pytest.approx(0.9)
    assert settings.rules.lot_size == 100
    assert settings.rules.t_plus_1 is False


@pytest.mark.parametrize(
    "payload, match",
    [
        ({"fill": "tomorrow"}, "fill"),
        ({"factor_source": "magic"}, "factor_source"),
        ({"corporate_actions": "maybe"}, "corporate_actions"),
        ({"max_gross_exposure": 1.5}, "max_gross_exposure"),
        ({"warmup_min_bars": -1}, "warmup_min_bars"),
        ({"outputs": []}, "outputs"),
    ],
)
def test_invalid_settings_values_raise(payload, match):
    with pytest.raises(ConfigError, match=match):
        BacktestSettings.from_dict(payload)


def test_invalid_stamp_tax_schedule_raises():
    with pytest.raises(ConfigError, match="stamp_tax_schedule"):
        ExecutionRules.from_dict({"stamp_tax_schedule": [{"effective_from": "2020-01-01"}]})
