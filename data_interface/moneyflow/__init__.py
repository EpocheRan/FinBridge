"""个股日频分档（资金流）数据接口。"""

from .service import MoneyflowService
from .sources.tushare_source import TushareMoneyflowSource

__all__ = ["MoneyflowService", "TushareMoneyflowSource"]
