import logging
import uuid
import json
from datetime import datetime
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker
from .models import Base, ProfolioModel, PositionModel, TradeRecordModel
from utils.DataConnect import questDB
from .modes.atr_mode import ATRMode

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("PositionManager")


class PositionManager:
    def __init__(self, data_loader=None):
        """Initialize the Position Manager with database connection."""
        self.data_loader = data_loader
        self.db = questDB()
        self.engine = self.db.pd_conn
        # Create tables if they don't exist
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _get_mode_instance(self, mode_name, params_str):
        """Helper to instantiate the mode class from database string"""
        params = json.loads(params_str) if params_str else {}
        if mode_name == "ATR":
            return ATRMode(**params)
        # Default fallback or other modes
        return ATRMode(**params)

    # --- 10) Create Profolio ---
    def create_profolio(self, name, cash=0.0):
        session = self.Session()
        try:
            profolio_id = str(uuid.uuid4())[:8]
            new_profolio = ProfolioModel(
                profolio_id=profolio_id,
                name=name,
                total_assets=cash,
                cash=cash,
                market_value=0.0,
                unrealized_profit=0.0,
                realized_profit=0.0,
                id=datetime.utcnow(),
            )
            session.add(new_profolio)
            session.commit()
            logger.info(f"Created profolio: {name} ({profolio_id})")
            return profolio_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create profolio: {e}")
            return None
        finally:
            session.close()

    # --- 11) Modify Profolio ---
    def modify_profolio(self, profolio_id, updates):
        session = self.Session()
        try:
            latest = (
                session.query(ProfolioModel)
                .filter_by(profolio_id=profolio_id)
                .order_by(desc(ProfolioModel.id))
                .first()
            )
            if not latest:
                logger.error(f"Profolio {profolio_id} not found")
                return False

            new_record = ProfolioModel(
                profolio_id=profolio_id,
                name=updates.get("name", latest.name),
                total_assets=updates.get("total_assets", latest.total_assets),
                cash=updates.get("cash", latest.cash),
                market_value=updates.get("market_value", latest.market_value),
                unrealized_profit=updates.get(
                    "unrealized_profit", latest.unrealized_profit
                ),
                realized_profit=updates.get("realized_profit", latest.realized_profit),
                id=datetime.utcnow(),
            )
            session.add(new_record)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to modify profolio: {e}")
            return False
        finally:
            session.close()

    # --- 12) Calculate Profolio ---
    def calculate_profolio(self, profolio_id):
        session = self.Session()
        try:
            # Get latest versions of all positions for this portfolio
            all_pos_ids = (
                session.query(PositionModel.pos_id)
                .filter_by(profolio_id=profolio_id)
                .distinct()
                .all()
            )

            total_market_value = 0.0
            total_unrealized_profit = 0.0
            total_realized_profit = 0.0

            latest_pro = (
                session.query(ProfolioModel)
                .filter_by(profolio_id=profolio_id)
                .order_by(desc(ProfolioModel.id))
                .first()
            )
            if not latest_pro:
                return False

            total_cash = latest_pro.cash

            for (pos_id,) in all_pos_ids:
                latest_pos = (
                    session.query(PositionModel)
                    .filter_by(pos_id=pos_id)
                    .order_by(desc(PositionModel.id))
                    .first()
                )
                if latest_pos:
                    total_market_value += latest_pos.market_value
                    total_unrealized_profit += latest_pos.unrealized_profit
                    total_realized_profit += latest_pos.realized_profit

            updates = {
                "market_value": total_market_value,
                "unrealized_profit": total_unrealized_profit,
                "realized_profit": total_realized_profit,
                "total_assets": total_cash + total_market_value,
            }
            session.close()
            return self.modify_profolio(profolio_id, updates)
        except Exception as e:
            logger.error(f"Failed to calculate profolio: {e}")
            return False
        finally:
            if session:
                session.close()

    # --- 13) List Profolios ---
    def list__profolio(self):
        session = self.Session()
        try:
            all_records = (
                session.query(ProfolioModel).order_by(desc(ProfolioModel.id)).all()
            )
            seen = set()
            latest_records = []
            for r in all_records:
                if r.profolio_id not in seen:
                    latest_records.append(r)
                    seen.add(r.profolio_id)
            return latest_records
        finally:
            session.close()

    # --- 1) Create Position ---
    def create_position(self, profolio_id, ticker, name, mode, mode_params=None):
        session = self.Session()
        try:
            pos_id = str(uuid.uuid4())[:8]
            now = datetime.utcnow()
            new_pos = PositionModel(
                pos_id=pos_id,
                profolio_id=profolio_id,
                ticker=ticker,
                name=name,
                mode=mode,
                created_at=now,
                updated_at=now,
                id=now,
                mode_params=json.dumps(mode_params) if mode_params else "{}",
            )
            session.add(new_pos)
            session.commit()
            logger.info(
                f"Created position: {ticker} ({pos_id}) in profolio {profolio_id}"
            )
            return pos_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create position: {e}")
            return None
        finally:
            session.close()

    # --- 6) Modify Position ---
    def modify_position(self, pos_id, updates):
        session = self.Session()
        try:
            latest = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id)
                .order_by(desc(PositionModel.id))
                .first()
            )
            if not latest:
                return False

            now = datetime.utcnow()
            new_record = PositionModel(
                pos_id=pos_id,
                profolio_id=latest.profolio_id,
                ticker=updates.get("ticker", latest.ticker),
                name=updates.get("name", latest.name),
                mode=updates.get("mode", latest.mode),
                created_at=latest.created_at,
                cost=updates.get("cost", latest.cost),
                market_value=updates.get("market_value", latest.market_value),
                unrealized_profit=updates.get(
                    "unrealized_profit", latest.unrealized_profit
                ),
                unrealized_profit_rate=updates.get(
                    "unrealized_profit_rate", latest.unrealized_profit_rate
                ),
                realized_profit=updates.get("realized_profit", latest.realized_profit),
                total_profit=updates.get("total_profit", latest.total_profit),
                mode_params=updates.get("mode_params", latest.mode_params),
                updated_at=now,
                id=now,
            )
            session.add(new_record)
            session.commit()
            pro_id = latest.profolio_id
            session.close()
            self.calculate_profolio(pro_id)
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to modify position: {e}")
            return False
        finally:
            if session:
                session.close()

    # --- 7) Delete Position ---
    def delete_position(self, pos_id, history_id=None):
        session = self.Session()
        try:
            if history_id:
                session.query(PositionModel).filter_by(
                    pos_id=pos_id, id=history_id
                ).delete()
            else:
                session.query(PositionModel).filter_by(pos_id=pos_id).delete()
                session.query(TradeRecordModel).filter_by(pos_id=pos_id).delete()
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to delete position: {e}")
            return False
        finally:
            session.close()

    # --- 9) List Positions ---
    def list_positions(self, profolio_id):
        session = self.Session()
        try:
            all_records = (
                session.query(PositionModel)
                .filter_by(profolio_id=profolio_id)
                .order_by(desc(PositionModel.id))
                .all()
            )
            seen = set()
            latest_records = []
            for r in all_records:
                if r.pos_id not in seen:
                    # Attach mode instance to the record object (volatile)
                    r.mode_instance = self._get_mode_instance(r.mode, r.mode_params)
                    latest_records.append(r)
                    seen.add(r.pos_id)
            return latest_records
        finally:
            session.close()

    # --- 2) Add Trade Record ---
    def add_trade_record(self, pos_id, trade_type, quantity, price, trade_date=None):
        session = self.Session()
        try:
            trade_id = str(uuid.uuid4())[:8]
            now = datetime.utcnow()
            new_trade = TradeRecordModel(
                trade_id=trade_id,
                pos_id=pos_id,
                type=trade_type,
                quantity=quantity,
                price=price,
                trade_date=trade_date or now,
                updated_at=now,
            )
            session.add(new_trade)
            session.commit()
            session.close()
            self.calculate_profit(pos_id)
            return trade_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to add trade: {e}")
            return None
        finally:
            if session:
                session.close()

    # --- 3) Modify Trade Record ---
    def modify_trade_record(self, trade_id, updates):
        session = self.Session()
        try:
            trade = session.query(TradeRecordModel).filter_by(trade_id=trade_id).first()
            if not trade:
                return False
            for key, val in updates.items():
                setattr(trade, key, val)
            trade.updated_at = datetime.utcnow()
            p_id = trade.pos_id
            session.commit()
            session.close()
            self.calculate_profit(p_id)
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to modify trade: {e}")
            return False
        finally:
            if session:
                session.close()

    # --- 4) Delete Trade Record ---
    def delete_trade_record(self, trade_id):
        session = self.Session()
        try:
            trade = session.query(TradeRecordModel).filter_by(trade_id=trade_id).first()
            if not trade:
                return False
            p_id = trade.pos_id
            session.delete(trade)
            session.commit()
            session.close()
            self.calculate_profit(p_id)
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to delete trade: {e}")
            return False
        finally:
            if session:
                session.close()

    # --- 8) List Trade Records ---
    def list_trade_record(self, pos_id):
        session = self.Session()
        try:
            return (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id)
                .order_by(TradeRecordModel.trade_date)
                .all()
            )
        finally:
            session.close()

    # --- 5) Calculate Profit ---
    def calculate_profit(self, pos_id):
        session = self.Session()
        try:
            trades = (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id)
                .order_by(TradeRecordModel.trade_date)
                .all()
            )
            latest_pos = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id)
                .order_by(desc(PositionModel.id))
                .first()
            )
            if not latest_pos:
                return False

            total_buy_qty = 0.0
            total_buy_cost = 0.0
            total_sell_qty = 0.0
            total_sell_proceeds = 0.0
            current_qty = 0.0

            for t in trades:
                if t.type == "买入":
                    total_buy_qty += t.quantity
                    total_buy_cost += t.quantity * t.price
                    current_qty += t.quantity
                else:
                    total_sell_qty += t.quantity
                    total_sell_proceeds += t.quantity * t.price
                    current_qty -= t.quantity

            avg_cost = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0.0
            latest_price = avg_cost  # Default

            # Use data_loader if available to get real-time price
            if self.data_loader and latest_pos.ticker:
                try:
                    price_df = self.data_loader.get_latest_price(latest_pos.ticker)
                    if not price_df.empty:
                        latest_price = price_df["close"].iloc[-1]
                except:
                    pass
            elif trades:
                latest_price = trades[-1].price

            market_value = current_qty * latest_price
            unrealized_profit = (latest_price - avg_cost) * current_qty
            unrealized_profit_rate = (
                (latest_price / avg_cost - 1) if avg_cost > 0 else 0.0
            )
            realized_profit = total_sell_proceeds - (total_sell_qty * avg_cost)
            total_profit = unrealized_profit + realized_profit

            updates = {
                "cost": avg_cost,
                "market_value": market_value,
                "unrealized_profit": unrealized_profit,
                "unrealized_profit_rate": unrealized_profit_rate,
                "realized_profit": realized_profit,
                "total_profit": total_profit,
            }
            session.close()
            return self.modify_position(pos_id, updates)
        except Exception as e:
            logger.error(f"Failed to calculate profit: {e}")
            return False
        finally:
            if session:
                session.close()
