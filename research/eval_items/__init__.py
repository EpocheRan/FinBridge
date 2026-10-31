"""评估项目录（G）：内置项在此注册，新增项只需 import 一次。"""

from .base import (
    EvalContext,
    ItemResult,
    ItemSpec,
    catalog,
    execute,
    item,
    resolve,
    schema_for,
)
from . import items  # noqa: F401 - 注册内置评估项
from . import stability  # noqa: F401 - 示例：分年度稳定性

__all__ = [
    "EvalContext",
    "ItemResult",
    "ItemSpec",
    "catalog",
    "execute",
    "item",
    "resolve",
    "schema_for",
]
