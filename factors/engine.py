"""轻量因子/指标计算引擎（纯 pandas）。

保留旧的 ``IndicatorEngine.calculate(data, IndicatorClass, **kwargs)`` 调用方式；
``FactorEngine`` 是同一实现的正式名称，``IndicatorEngine`` 作为兼容别名。
"""

from __future__ import annotations

import pandas as pd


class IndicatorEngine:
    """从 OHLCV DataFrame 计算单标的指标/因子。"""

    def calculate(self, data: pd.DataFrame, indicator_cls, **kwargs) -> pd.DataFrame:
        if not isinstance(data, pd.DataFrame):
            raise TypeError("data must be a pandas DataFrame")
        if not callable(indicator_cls):
            raise TypeError("indicator_cls must be callable")

        indicator = indicator_cls(**kwargs)
        result = indicator.calculate(data)

        if isinstance(result, pd.DataFrame):
            return result
        if isinstance(result, pd.Series):
            name = result.name or "value"
            return result.to_frame(name=name)
        return pd.DataFrame(result, index=data.index)

    def batch_calculate(
        self, data_dict: dict[str, pd.DataFrame], indicator_cls, **kwargs
    ) -> dict[str, pd.DataFrame]:
        return {
            symbol: self.calculate(frame, indicator_cls, **kwargs)
            for symbol, frame in data_dict.items()
        }


#: 正式名称；旧代码继续可用 ``IndicatorEngine``
FactorEngine = IndicatorEngine
