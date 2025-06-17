import logging
# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('factor')

# 导入DataConnect.py中的类
from .Alpha101 import *

try:
    from .Alpha101 import __all__ as alpha_101
    __all__ = list(alpha_101)
except ImportError:
    logger.error("初始化模块错误")