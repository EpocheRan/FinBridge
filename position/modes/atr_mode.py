from .base import PositionModeBase
import json
from ..models import PositionModel
from sqlalchemy import desc


class ATRMode(PositionModeBase):
    def __init__(
        self,
        stop_price=0.0,
        initial_atr=0.0,
        n_factor=2.0,
        time_stop_days=5,
        risk_per_trade=0.02,
    ):
        super().__init__(stop_price)
        self.initial_atr = initial_atr
        self.n_factor = n_factor
        self.time_stop_days = time_stop_days
        self.risk_per_trade = risk_per_trade

    def calculate_stop_price(self, position_info):
        """目前暂不实现"""
        return self.stop_price

    def get_mode_data(self):
        """返回当前模式的所有属性"""
        return {
            "stop_price": self.stop_price,
            "initial_atr": self.initial_atr,
            "n_factor": self.n_factor,
            "time_stop_days": self.time_stop_days,
            "risk_per_trade": self.risk_per_trade,
        }

    def modify_mode_data(self, updates):
        """修改对象中的属性"""
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def save_mode_data(self, session, pos_id):
        """将当前模式属性保存到数据库中的 latest position 记录"""
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
        """目前暂不实现"""
        return 0

    def check_reduce_signal(self, position_info):
        """目前暂不实现"""
        return 0.0

    def check_add_signal(self, position_info):
        """目前暂不实现"""
        return 0.0

    def calculate_purchase_advice(self, ohlcv_data):
        """目前暂不实现"""
        return {"quantity": 0, "stop_price": self.stop_price}
