from sqlalchemy import Column, String, Float, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
import uuid

Base = declarative_base()


class PortfolioModel(Base):
    __tablename__ = "portfolios"
    id = Column(DateTime, primary_key=True, default=datetime.utcnow)
    portfolio_id = Column(String(50), unique=True, nullable=False)
    name = Column(String(100))
    total_assets = Column(Float, default=0.0)
    cash = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    unrealized_profit = Column(Float, default=0.0)
    realized_profit = Column(Float, default=0.0)
    deleted_at = Column(DateTime, default=None)


class PositionModel(Base):
    __tablename__ = "positions"
    id = Column(DateTime, primary_key=True, default=datetime.utcnow)
    pos_id = Column(String(50), unique=True, nullable=False)
    portfolio_id = Column(String(50), index=True)
    ticker = Column(String(20))
    name = Column(String(100))
    mode = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    cost = Column(Float, default=0.0)
    quantity = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    unrealized_profit = Column(Float, default=0.0)
    unrealized_profit_rate = Column(Float, default=0.0)
    realized_profit = Column(Float, default=0.0)
    total_profit = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)
    mode_params = Column(String(1000), default="{}")
    deleted_at = Column(DateTime, default=None)
    # 止损价作为基础属性
    stop_price = Column(Float, default=0.0)
    # 首次建仓日期
    first_entry_date = Column(DateTime, default=None)


class TradeRecordModel(Base):
    __tablename__ = "trade_records"
    trade_id = Column(String(50), primary_key=True)
    pos_id = Column(String(50), index=True)
    type = Column(String(10))
    quantity = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    trade_date = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, default=None)


def create_tables(engine):
    """Create all tables defined in this module"""
    Base.metadata.create_all(engine)
