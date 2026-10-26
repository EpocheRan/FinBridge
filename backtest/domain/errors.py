class BacktestError(Exception):
    """回测管线的基础异常。"""


class ConfigError(BacktestError):
    """配置不合法。"""


class DataError(BacktestError):
    """数据源不可用或数据不足。"""


class StrategyLoadError(BacktestError):
    """策略文件加载/校验失败。"""


from factors.errors import FactorError


class ExecutionError(BacktestError):
    """执行/撮合失败。"""


class EngineError(BacktestError):
    """回测引擎失败。"""
