"""轻量指标计算引擎（纯 pandas）。

保留旧的 ``IndicatorEngine.calculate(data, IndicatorClass, **kwargs)`` 调用方式；
具体指标只需实现 ``calculate(data) -> Series | DataFrame``。
"""

from __future__ import annotations

import pandas as pd


class IndicatorEngine:
    """从 OHLCV DataFrame 计算单标的指标。"""

    def calculate(self, data: pd.DataFrame, indicator_cls, **kwargs) -> pd.DataFrame:
        """计算指标并返回与输入索引对齐的 DataFrame。

        Args:
            data: 含 ``open/high/low/close/volume`` 的 DataFrame。
            indicator_cls: 指标类，需实现 ``calculate(data)``。
            kwargs: 传给指标类构造函数的关键字参数。
        """
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
        """对多只股票批量计算。"""
        return {
            symbol: self.calculate(frame, indicator_cls, **kwargs)
            for symbol, frame in data_dict.items()
        }
