from sqlalchemy import Column, String, Float, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class ProfolioModel(Base):
    __tablename__ = "profolios"
    id = Column(
        DateTime, primary_key=True, default=datetime.utcnow
    )  # Use time as part of PK for history
    profolio_id = Column(String(50), primary_key=True)
    name = Column(String(100))
    total_assets = Column(Float, default=0.0)
    cash = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    unrealized_profit = Column(Float, default=0.0)
    realized_profit = Column(Float, default=0.0)


class PositionModel(Base):
    __tablename__ = "positions"
    id = Column(
        DateTime, primary_key=True, default=datetime.utcnow
    )  # Use time as part of PK for history
    pos_id = Column(String(50), primary_key=True)
    profolio_id = Column(String(50), index=True)
    ticker = Column(String(20))
    name = Column(String(100))
    mode = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    cost = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    unrealized_profit = Column(Float, default=0.0)
    unrealized_profit_rate = Column(Float, default=0.0)
    realized_profit = Column(Float, default=0.0)
    total_profit = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)
    mode_params = Column(
        String(1000), default="{}"
    )  # Store mode attributes as JSON string


class TradeRecordModel(Base):
    __tablename__ = "trade_records"
    trade_id = Column(String(50), primary_key=True)
    pos_id = Column(String(50), index=True)
    type = Column(String(10))  # 买入/卖出
    quantity = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    trade_date = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
