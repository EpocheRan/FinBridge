from abc import ABC, abstractmethod
import json


class PositionModeBase(ABC):
    def __init__(self, stop_price=0.0):
        self.stop_price = stop_price

    @abstractmethod
    def calculate_stop_price(self, position_info):
        """a. 计算止损价格：输入持仓信息，返回一个价格"""
        pass

    @abstractmethod
    def get_mode_data(self):
        """b. 获取模式数据，返回属性信息"""
        pass

    @abstractmethod
    def modify_mode_data(self, updates):
        """c. 修改模式数据，调整对象中的属性"""
        pass

    @abstractmethod
    def save_mode_data(self, session, pos_id):
        """d. 保存模式数据，向数据库保存当前模式的信息"""
        pass

    @abstractmethod
    def check_risk(self, position_info):
        """e. 检查仓位风险，返回风险等级0-5"""
        pass

    @abstractmethod
    def check_reduce_signal(self, position_info):
        """f. 检查减持信号，返回应减持的比例"""
        pass

    @abstractmethod
    def check_add_signal(self, position_info):
        """g. 检查增持信号，返回应增持的比例"""
        pass

    @abstractmethod
    def calculate_purchase_advice(self, ohlcv_data):
        """h. 计算购买建议：输入ohlcv信息，计算最佳持仓数量与止损价格"""
        pass
