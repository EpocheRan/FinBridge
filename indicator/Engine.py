import backtrader as bt
import pandas as pd
import numpy as np


class IndicatorEngine:
    """
    Engine to calculate indicators from OHLCV DataFrame using Backtrader indicators.
    """

    def __init__(self):
        pass

    def calculate(
        self, data: pd.DataFrame, indicator_cls: bt.Indicator, **kwargs
    ) -> pd.DataFrame:
        """
        Calculate indicator values for the given data.

        Args:
            data: DataFrame with index as datetime and columns 'open', 'high', 'low', 'close', 'volume'
            indicator_cls: The Backtrader Indicator class to use
            kwargs: Parameters for the indicator

        Returns:
            DataFrame with the indicator lines as columns, indexed by datetime.
        """

        # 1. Setup Cerebro with no stdstats (faster)
        cerebro = bt.Cerebro(stdstats=False)

        # 2. Add Data
        # Ensure data is standard. If not, map columns.
        # Assuming standard 'open', 'high', 'low', 'close', 'volume' lowercase or capitalized
        if "datetime" not in data.columns and not isinstance(
            data.index, pd.DatetimeIndex
        ):
            raise ValueError("Data must have a datetime index or column")

        data_feed = bt.feeds.PandasData(dataname=data)
        cerebro.adddata(data_feed)

        # 3. Create a Strategy wrapper to hold the indicator
        results = {}

        class IndicatorStrategy(bt.Strategy):
            def __init__(self):
                self.ind = indicator_cls(self.data, **kwargs)

            def next(self):
                # We extract data after run, or we could collect here.
                # Since we want a DF aligned with input, we can just access line objects later.
                pass

        cerebro.addstrategy(IndicatorStrategy)

        # 4. Run
        strats = cerebro.run()
        strat = strats[0]

        # 5. Extract Indicator Lines
        # The indicator instance is strat.ind
        ind = strat.ind

        output_data = {}
        aliases = ind.lines.getlinealiases()

        # Iterate over lines defined in the indicator
        for line_i in range(ind.lines.size()):
            line_name = aliases[line_i] if line_i < len(aliases) else f"line_{line_i}"

            # Extract the full line data as a list then convert to numpy
            # BT lines are 0-indexed and contain all points after run
            line_data = ind.lines[line_i].get(size=len(data))
            output_data[line_name] = np.array(line_data)

        # 6. Construct DataFrame
        # Backtrader arrays might be slightly different length or padded.
        # usually they match the data length.

        # The internal array is a standard array.array or numpy array usually.
        # We need to ensure it aligns with the input index.

        df_out = pd.DataFrame(output_data, index=data.index)

        # Handle NaNs from lookback period (Backtrader might leave them as 0 or NaN depending on impl,
        # usually NaN if using standard numpy interface or masked).
        # Actually standard BT array uses NaN for non-computed values if using numpy,
        # but pure python BT uses a specific value.
        # Let's assume numpy based backtrader (which is default if numpy installed).

        return df_out

    def batch_calculate(
        self, data_dict: dict, indicator_cls: bt.Indicator, **kwargs
    ) -> dict:
        """
        Run calculation for multiple stocks.

        Args:
            data_dict: {stock_id: DataFrame}

        Returns:
            {stock_id: ResultDataFrame}
        """
        results = {}
        for stock, df in data_dict.items():
            results[stock] = self.calculate(df, indicator_cls, **kwargs)
        return results
