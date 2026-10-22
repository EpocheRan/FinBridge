"""持仓模式注册表 / 工厂。

新增一种持仓策略只需：
1. 在 ``position/modes/`` 下新建一个继承 ``PositionModeBase`` 的类；
2. 在下面的 ``MODE_REGISTRY`` 里登记一个名字。

之后 ``PositionManager`` 与各接口层都通过 ``create_mode`` 透明地按名称实例化，
无需再改动任何 ``if mode == "XXX"`` 分支。
"""

import inspect
import logging

from .base import PositionModeBase
from .atr_mode import ATRMode
from .grid_mode import GridMode

logger = logging.getLogger("ModeRegistry")

# 模式名（大写） -> 模式类
MODE_REGISTRY = {
    "ATR": ATRMode,
    "GRID": GridMode,
}

DEFAULT_MODE = "ATR"


def get_mode_class(mode_name: str):
    """按名称返回模式类；未知名称回退默认模式。"""
    if not mode_name:
        return MODE_REGISTRY[DEFAULT_MODE]
    key = str(mode_name).upper()
    if key not in MODE_REGISTRY:
        logger.warning(f"未知模式 '{mode_name}'，回退到 {DEFAULT_MODE}")
        return MODE_REGISTRY[DEFAULT_MODE]
    return MODE_REGISTRY[key]


def create_mode(mode_name: str, params: dict = None) -> PositionModeBase:
    """按名称 + 参数（通常是 mode_params）实例化模式。

    会自动过滤掉目标模式 ``__init__`` 不接受的 key，这样不同模式的
    mode_params 混用（例如 grid 里残留 initial_atr）也不会报错。
    """
    cls = get_mode_class(mode_name)
    params = params or {}
    accepted = set(inspect.signature(cls.__init__).parameters) - {"self"}
    filtered = {k: v for k, v in params.items() if k in accepted}
    return cls(**filtered)


__all__ = [
    "PositionModeBase",
    "ATRMode",
    "GridMode",
    "MODE_REGISTRY",
    "DEFAULT_MODE",
    "get_mode_class",
    "create_mode",
]
