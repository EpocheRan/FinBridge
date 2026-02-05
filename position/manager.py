import logging
import uuid
import json
import time
from datetime import datetime
from sqlalchemy import create_engine, desc, text
from sqlalchemy.orm import sessionmaker
from .schema import Base, PortfolioModel, PositionModel, TradeRecordModel, create_tables
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
            # Check for existing portfolio with the same name to prevent duplicates
            existing = (
                session.query(PortfolioModel)
                .filter_by(name=name)
                .filter_by(deleted_at=None)
                .first()
            )
            if existing:
                logger.info(
                    f"Portfolio with name '{name}' already exists. Returning existing ID: {existing.portfolio_id}"
                )
                return existing.portfolio_id

            portfolio_id = str(uuid.uuid4())[:8]
            new_profolio = PortfolioModel(
                portfolio_id=portfolio_id,
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
            logger.info(f"Created profolio: {name} ({portfolio_id})")
            return portfolio_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create profolio: {e}")
            return None
        finally:
            session.close()

    # --- 11) Modify Profolio ---
    def modify_profolio(self, portfolio_id, updates):
        try:
            # 构建 SET 子句
            set_parts = []
            for key, val in updates.items():
                if isinstance(val, str):
                    set_parts.append(f"{key} = '{val}'")
                elif isinstance(val, (int, float)):
                    set_parts.append(f"{key} = {val}")
                # 忽略 None 值，或者根据需求处理

            if not set_parts:
                return True

            set_clause = ", ".join(set_parts)

            # 使用 REST API 执行 UPDATE
            # 为了确保只更新最新的那条（如果存在多条历史），最好指定 ID，或者如果目标是全量更新则直接用 portfolio_id
            # 这里我们使用 portfolio_id 更新所有记录（符合"修改同一条数据"的语义，即不再区分历史版本）
            # 注意：这会更新该 portfolio_id 下的所有记录

            sql = f"UPDATE portfolios SET {set_clause} WHERE portfolio_id = '{portfolio_id}'"

            result = self.db.execute_raw_sql(sql)
            if result:
                time.sleep(0.6)  # Wait for WAL commit
                logger.info(f"Modified portfolio {portfolio_id} via REST API")
                return True
            else:
                logger.error(f"Failed to modify portfolio {portfolio_id} via REST API")
                return False

        except Exception as e:
            logger.error(f"Failed to modify profolio: {e}")
            return False

    # --- 12) Calculate Profolio ---
    def calculate_profolio(self, portfolio_id):
        session = self.Session()
        try:
            # Get latest versions of all positions for this portfolio
            all_pos_ids = (
                session.query(PositionModel.pos_id)
                .filter_by(portfolio_id=portfolio_id)
                .filter_by(deleted_at=None)
                .distinct()
                .all()
            )

            total_market_value = 0.0
            total_unrealized_profit = 0.0
            total_realized_profit = 0.0

            latest_pro = (
                session.query(PortfolioModel)
                .filter_by(portfolio_id=portfolio_id)
                .filter_by(deleted_at=None)
                .order_by(desc(PortfolioModel.id))
                .first()
            )
            if not latest_pro:
                return False

            total_cash = latest_pro.cash

            for (pos_id,) in all_pos_ids:
                latest_pos = (
                    session.query(PositionModel)
                    .filter_by(pos_id=pos_id)
                    .filter_by(deleted_at=None)
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
            return self.modify_profolio(portfolio_id, updates)
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
                session.query(PortfolioModel)
                .filter_by(deleted_at=None)
                .order_by(PortfolioModel.id)
                .all()
            )
            seen = set()
            latest_records = []
            for r in all_records:
                if r.portfolio_id not in seen:
                    latest_records.append(r)
                    seen.add(r.portfolio_id)
            return latest_records
        finally:
            session.close()

    # --- 14) Delete Profolio ---
    def delete_profolio(self, portfolio_id):
        try:
            # Use soft delete via REST API (UPDATE instead of DELETE)
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")

            # 1. Get all position IDs first for manual cascade
            session = self.Session()
            result = (
                session.query(PositionModel.pos_id)
                .filter_by(portfolio_id=portfolio_id)
                .filter_by(deleted_at=None)
                .distinct()
                .all()
            )
            pos_ids = [row[0] for row in result]
            session.close()

            # 2. Soft delete Trade Records
            for pid in pos_ids:
                self.db.execute_raw_sql(
                    f"UPDATE trade_records SET deleted_at = '{now}' WHERE pos_id = '{pid}' AND deleted_at IS NULL"
                )

            # 3. Soft delete Positions
            self.db.execute_raw_sql(
                f"UPDATE positions SET deleted_at = '{now}' WHERE portfolio_id = '{portfolio_id}' AND deleted_at IS NULL"
            )

            # 4. Soft delete Portfolio records
            self.db.execute_raw_sql(
                f"UPDATE portfolios SET deleted_at = '{now}' WHERE portfolio_id = '{portfolio_id}' AND deleted_at IS NULL"
            )

            logger.info(f"Soft deleted portfolio: {portfolio_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete portfolio: {e}")
            return False

    # --- Helper Methods for LLM Interface ---

    def get_position_by_ticker(self, portfolio_id, ticker):
        """Helper: Find the latest position record by ticker symbol."""
        positions = self.list_positions(portfolio_id)
        for p in positions:
            if p.ticker == ticker:
                return p
        return None

    def refresh_all_positions(self, portfolio_id):
        """Force recalculate profit/market value for all positions in portfolio."""
        positions = self.list_positions(portfolio_id)
        for p in positions:
            # calculate_profit fetches price internally now
            self.calculate_profit(p.pos_id)
        # Recalculate total portfolio stats
        self.calculate_profolio(portfolio_id)
        return True

    def get_portfolio_summary(self, portfolio_id):
        """Get a full summary of the portfolio for LLM context."""
        # 1. Refresh Data
        self.refresh_all_positions(portfolio_id)

        # 2. Get Portfolio Info
        session = self.Session()
        try:
            latest_pro = (
                session.query(PortfolioModel)
                .filter_by(portfolio_id=portfolio_id)
                .filter_by(deleted_at=None)
                .order_by(desc(PortfolioModel.id))
                .first()
            )
            if not latest_pro:
                return {"error": "Portfolio not found"}

            positions = self.list_positions(portfolio_id)

            pos_list = []
            for p in positions:
                mode_params = json.loads(p.mode_params or "{}")
                pos_list.append(
                    {
                        "pos_id": p.pos_id,
                        "ticker": p.ticker,
                        "name": p.name,
                        "quantity": p.quantity,
                        "cost_price": float(p.cost) if p.cost else 0.0,
                        "current_price": float(p.market_value / p.quantity)
                        if p.quantity
                        else 0.0,
                        "market_value": float(p.market_value)
                        if p.market_value
                        else 0.0,
                        "unrealized_pnl": float(p.unrealized_profit)
                        if p.unrealized_profit
                        else 0.0,
                        "return_rate": f"{float(p.unrealized_profit_rate * 100) if p.unrealized_profit_rate else 0.0:.2f}%",
                        "mode": p.mode,
                        "stop_price": float(p.stop_price) if p.stop_price else None,
                        "initial_atr": float(mode_params.get("initial_atr"))
                        if mode_params.get("initial_atr")
                        else None,
                        "n_factor": float(mode_params.get("n_factor"))
                        if mode_params.get("n_factor")
                        else None,
                        "risk_per_trade": float(mode_params.get("risk_per_trade"))
                        if mode_params.get("risk_per_trade")
                        else None,
                        "time_stop_days": int(mode_params.get("time_stop_days"))
                        if mode_params.get("time_stop_days")
                        else None,
                        "first_entry_date": p.first_entry_date.isoformat()
                        if p.first_entry_date
                        else None,
                        "mode_params": mode_params,
                    }
                )

            return {
                "portfolio_name": latest_pro.name,
                "total_assets": round(latest_pro.total_assets, 2),
                "cash": round(latest_pro.cash, 2),
                "market_value": round(latest_pro.market_value, 2),
                "total_unrealized_pnl": round(latest_pro.unrealized_profit, 2),
                "positions": pos_list,
            }
        finally:
            session.close()

    # --- 1) Create Position ---
    def create_position(self, portfolio_id, ticker, name, mode, mode_params=None):
        session = self.Session()
        try:
            pos_id = str(uuid.uuid4())[:8]
            now = datetime.utcnow()
            new_pos = PositionModel(
                pos_id=pos_id,
                portfolio_id=portfolio_id,
                ticker=ticker,
                name=name,
                mode=mode,
                created_at=now,
                updated_at=now,
                id=now,
                quantity=0.0,
                mode_params=json.dumps(mode_params) if mode_params else "{}",
            )
            session.add(new_pos)
            session.commit()
            logger.info(
                f"Created position: {ticker} ({pos_id}) in profolio {portfolio_id}"
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
        try:
            # 获取 portfolio_id 以便重新计算
            session = self.Session()
            pos = session.query(PositionModel).filter_by(pos_id=pos_id).first()
            if not pos:
                session.close()
                return False
            pro_id = pos.portfolio_id
            session.close()

            # 构建 SET 子句
            set_parts = []
            for key, val in updates.items():
                if key == "mode_params" and isinstance(val, dict):
                    val = json.dumps(val)

                if isinstance(val, str):
                    set_parts.append(f"{key} = '{val}'")
                elif isinstance(val, (int, float)):
                    set_parts.append(f"{key} = {val}")

            if not set_parts:
                return True

            set_clause = ", ".join(set_parts)

            sql = f"UPDATE positions SET {set_clause} WHERE pos_id = '{pos_id}'"

            success = self.db.execute_raw_sql(sql)

            if success:
                time.sleep(0.6)  # Wait for WAL commit
                self.calculate_profolio(pro_id)
                return True
            return False

        except Exception as e:
            logger.error(f"Failed to modify position: {e}")
            return False

    # --- 7) Delete Position ---
    def delete_position(self, pos_id, history_id=None):
        try:
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")

            if history_id:
                # Soft delete specific snapshot
                self.db.execute_raw_sql(
                    f"UPDATE positions SET deleted_at = '{now}' WHERE pos_id = '{pos_id}' AND id = '{history_id}'"
                )
            else:
                # Soft delete position and all related trades
                self.db.execute_raw_sql(
                    f"UPDATE trade_records SET deleted_at = '{now}' WHERE pos_id = '{pos_id}' AND deleted_at IS NULL"
                )
                self.db.execute_raw_sql(
                    f"UPDATE positions SET deleted_at = '{now}' WHERE pos_id = '{pos_id}' AND deleted_at IS NULL"
                )
            return True
        except Exception as e:
            logger.error(f"Failed to delete position: {e}")
            return False

    # --- 9) List Positions ---
    def list_positions(self, portfolio_id):
        session = self.Session()
        try:
            all_records = (
                session.query(PositionModel)
                .filter_by(portfolio_id=portfolio_id)
                .filter_by(deleted_at=None)
                .order_by(PositionModel.id)
                .all()
            )
            seen = set()
            latest_records = []
            for r in all_records:
                if r.pos_id not in seen:
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
            # Check if this is the first buy trade (for setting first_entry_date)
            existing_trades = (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id)
                .filter_by(deleted_at=None)
                .all()
            )
            is_first_trade = len(existing_trades) == 0

            trade_id = str(uuid.uuid4())[:8]
            now = datetime.utcnow()
            actual_trade_date = trade_date or now

            new_trade = TradeRecordModel(
                trade_id=trade_id,
                pos_id=pos_id,
                type=trade_type,
                quantity=quantity,
                price=price,
                trade_date=actual_trade_date,
                updated_at=now,
            )
            session.add(new_trade)
            session.commit()
            session.close()
            time.sleep(0.6)  # Wait for WAL commit

            # If this is the first buy trade, update first_entry_date
            if is_first_trade and trade_type == "买入":
                self._update_first_entry_date(pos_id, actual_trade_date)

            self.calculate_profit(pos_id)
            return trade_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to add trade: {e}")
            return None
        finally:
            if session:
                session.close()

    def _update_first_entry_date(self, pos_id, first_entry_date):
        """Update first_entry_date for a position"""
        try:
            date_str = first_entry_date.strftime("%Y-%m-%dT%H:%M:%S.%f")
            sql = f"UPDATE positions SET first_entry_date = '{date_str}' WHERE pos_id = '{pos_id}'"

            if self.db.execute_raw_sql(sql):
                logger.info(
                    f"Updated first_entry_date for {pos_id}: {first_entry_date}"
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to update first_entry_date: {e}")
            return False

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
            session.close()

            # Use soft delete via REST API
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")
            success = self.db.execute_raw_sql(
                f"UPDATE trade_records SET deleted_at = '{now}' WHERE trade_id = '{trade_id}'"
            )
            if success:
                self.calculate_profit(p_id)
            return success
        except Exception as e:
            logger.error(f"Failed to delete trade: {e}")
            return False

    # --- 8) List Trade Records ---
    def list_trade_record(self, pos_id):
        session = self.Session()
        try:
            return (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id)
                .filter_by(deleted_at=None)
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
                .filter_by(deleted_at=None)
                .order_by(TradeRecordModel.trade_date)
                .all()
            )
            latest_pos = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id)
                .filter_by(deleted_at=None)
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
            price_found = False
            if self.data_loader and latest_pos.ticker:
                try:
                    from datetime import datetime, timedelta

                    end_str = datetime.now().strftime("%Y-%m-%d")
                    start_str = (datetime.now() - timedelta(days=10)).strftime(
                        "%Y-%m-%d"
                    )
                    data_map = self.data_loader.ohlc(
                        latest_pos.ticker, start_str, end_str, freq="d", now=True
                    )
                    price_df = data_map.get(latest_pos.ticker)

                    if price_df is not None and not price_df.empty:
                        latest_price = price_df["close"].iloc[-1]
                        price_found = True
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch price for {latest_pos.ticker}: {e}"
                    )

            if not price_found and trades:
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
                "quantity": current_qty,
                "market_value": market_value,
                "unrealized_profit": unrealized_profit,
                "unrealized_profit_rate": unrealized_profit_rate,
                "realized_profit": realized_profit,
                "total_profit": total_profit,
            }
            session.close()

            # 调用 modify_position 更新基础数据
            result = self.modify_position(pos_id, updates)

            # 如果有数据加载器，尝试计算并保存新的 stop_price 和 initial_atr
            if result and self.data_loader and latest_pos.ticker:
                try:
                    # 创建 mode 实例
                    mode_params = json.loads(latest_pos.mode_params or "{}")
                    mode = ATRMode(**mode_params)

                    # 确定计算ATR的日期范围
                    from datetime import timedelta

                    # 如果有首次建仓日期，从建仓日期开始计算ATR
                    if latest_pos.first_entry_date:
                        entry_date = latest_pos.first_entry_date
                        if isinstance(entry_date, str):
                            entry_date = datetime.fromisoformat(
                                entry_date.replace("Z", "+00:00")
                            )
                        # 建仓日期往前推14天作为起始日期
                        start_str = (entry_date - timedelta(days=20)).strftime(
                            "%Y-%m-%d"
                        )
                        end_str = entry_date.strftime("%Y-%m-%d")
                        logger.info(
                            f"Calculating ATR from first_entry_date: {start_str} to {end_str}"
                        )
                    else:
                        # 默认使用最近10天
                        end_str = datetime.now().strftime("%Y-%m-%d")
                        start_str = (datetime.now() - timedelta(days=10)).strftime(
                            "%Y-%m-%d"
                        )

                    data_map = self.data_loader.ohlc(
                        latest_pos.ticker, start_str, end_str, freq="d", now=True
                    )
                    price_df = data_map.get(latest_pos.ticker)

                    if price_df is not None and not price_df.empty:
                        current_price = float(price_df["close"].iloc[-1])

                        # 计算建仓日的ATR值
                        if latest_pos.first_entry_date:
                            # 计算ATR (默认使用14天EMA)
                            atr_val = mode._calculate_atr_from_ohlcv(price_df)
                            if atr_val > 0:
                                # 更新 initial_atr
                                mode_params["initial_atr"] = atr_val
                                self._save_initial_atr(pos_id, atr_val)
                                logger.info(
                                    f"Set initial_atr for {pos_id} from entry date: {atr_val}"
                                )

                        # 计算新的止损价
                        position_info = {
                            "current_price": current_price,
                            "ohlcv": price_df,
                        }
                        new_stop_price = mode.calculate_stop_price(position_info)

                        # 保存 stop_price 到数据库
                        self.save_stop_price(pos_id, new_stop_price, mode_params)
                        logger.info(
                            f"Updated stop_price for {pos_id}: {new_stop_price}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to update stop_price: {e}")

            return result
        except Exception as e:
            logger.error(f"Failed to calculate profit: {e}")
            return False
        finally:
            if session:
                session.close()

    def _save_initial_atr(self, pos_id, atr_val):
        """保存 initial_atr 到 mode_params"""
        try:
            # 需要先读取现有的 mode_params
            session = self.Session()
            pos = session.query(PositionModel).filter_by(pos_id=pos_id).first()
            if not pos:
                session.close()
                return False

            mode_params = json.loads(pos.mode_params or "{}")
            session.close()

            mode_params["initial_atr"] = float(atr_val)
            params_str = json.dumps(mode_params)

            # 使用 SQL Update，注意 json 字符串中的引号需要转义
            # 这里简单的 replace "'" 可能不够，但 mode_params 主要是数字，应该还好
            # 使用 execute_raw_sql 的参数化替换更安全

            sql = f"UPDATE positions SET mode_params = :params WHERE pos_id = :pid"

            if self.db.execute_raw_sql(sql, {"params": params_str, "pid": pos_id}):
                time.sleep(0.6)  # Wait for WAL commit
                return True
            return False

        except Exception as e:
            logger.error(f"Failed to save initial_atr: {e}")
            return False

    # --- Save Stop Price ---
    def save_stop_price(self, pos_id, stop_price, mode_params=None):
        """
        保存或更新 position 的 stop_price 到基础字段和 mode_params 中。
        """
        try:
            if mode_params is None:
                session = self.Session()
                pos = session.query(PositionModel).filter_by(pos_id=pos_id).first()
                if not pos:
                    session.close()
                    return False
                mode_params = json.loads(pos.mode_params or "{}")
                session.close()
            else:
                mode_params = json.loads(json.dumps(mode_params))

            mode_params["stop_price"] = stop_price
            params_str = json.dumps(mode_params)

            sql = f"UPDATE positions SET stop_price = {stop_price}, mode_params = :params WHERE pos_id = :pid"

            if self.db.execute_raw_sql(sql, {"params": params_str, "pid": pos_id}):
                time.sleep(0.6)  # Wait for WAL commit
                return True
            return False

        except Exception as e:
            logger.error(f"Failed to save stop_price: {e}")
            return False
