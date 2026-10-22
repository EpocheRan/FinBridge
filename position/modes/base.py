import json
from sqlalchemy import desc
from ..schema import PositionModel


class PositionModeBase:
    def __init__(self, stop_price=0.0):
        self.stop_price = stop_price

    def calculate_stop_price(self, position_info):
        """a. 计算止损价格：输入持仓信息，返回一个价格"""
        pass

    def get_mode_data(self):
        """b. 获取模式数据，返回属性信息"""
        return {"stop_price": self.stop_price}

    def modify_mode_data(self, updates):
        """c. 修改模式数据，调整对象中的属性"""
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def save_mode_data(self, session, pos_id):
        """d. 保存模式数据，向数据库保存当前模式的信息"""
        latest = (
            session.query(PositionModel)
            .filter_by(pos_id=pos_id)
            .order_by(desc(PositionModel.id))
            .first()
        )
        if latest:
            data = self.get_mode_data()
            latest.mode_params = json.dumps(data)
            session.commit()
            return True
        return False

    def check_risk(self, position_info):
        """e. 检查仓位风险，返回风险等级0-5"""
        return 0

    def check_reduce_signal(self, position_info):
        """f. 检查减持信号，返回应减持的比例"""
        return 0.0

    def check_add_signal(self, position_info):
        """g. 检查增持信号，返回增持详情或比例"""
        return {}

    def calculate_purchase_advice(self, data):
        """h. 计算购买建议：输入数据（含ohlcv、资金等），计算最佳持仓数量与止损价格"""
        return {"quantity": 0, "stop_price": self.stop_price}

    def advance_level(self, trade_type, price, quantity, had_position=False):
        """i. 成交后推进模式内部层级（如金字塔加仓层、网格当前格）。

        默认无操作；具体模式按自身语义实现。
        """
        return None
