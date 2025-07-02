import logging
# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('factor')

# 导入DataConnect.py中的类
from .Alpha101 import *
from .common import *

try:
    from .Alpha101 import __all__ as alpha_101
    from .common import __all__ as common_module
    __all__ = alpha_101 + common_module
    
except ImportError:
    logger.error("初始化模块错误")