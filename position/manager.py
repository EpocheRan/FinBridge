"""持仓账本管理器。

2026-10 修订要点：

- 所有 UPDATE 改为**参数化 SQL**（``db.execute(sql, params)``），不再拼字符串，
  股票名带单引号也不会写坏库，且没有注入面。
- 去掉每次写库的 ``time.sleep(0.6)``：positions/portfolios 是非 WAL 表，
  UPDATE 提交即可见（WAL 才需要等待可见）。
- 时间统一 ``now_cn()``（北京时间 naive），与 ``trade_date`` 语义一致。
- 盈亏按**移动加权平均成本**逐笔推导：支持部分卖出、清仓后再买入；
  不复权现价 vs 不复权成本，ATR 用前复权序列（除权日不再有假跳波）。
- 卖出数量在写入流水前校验，禁止负持仓。
- 一次状态查询只拉**一次**行情（批量），不再每只仓位各拉一次。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import desc
from sqlalchemy.orm import sessionmaker

from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

from .modes import create_mode
from .schema import PortfolioModel, PositionModel, TradeRecordModel

logger = logging.getLogger("PositionManager")

BUY = "买入"
SELL = "卖出"
ADJUST = "调整"

_PORTFOLIO_UPDATABLE = {
    "name",
    "cash",
    "market_value",
    "unrealized_profit",
    "realized_profit",
    "total_assets",
}
_POSITION_UPDATABLE = {
    "name",
    "mode",
    "cost",
    "quantity",
    "market_value",
    "unrealized_profit",
    "unrealized_profit_rate",
    "realized_profit",
    "total_profit",
    "mode_params",
    "stop_price",
    "first_entry_date",
    "updated_at",
}
_TRADE_UPDATABLE = {"type", "quantity", "price", "trade_date", "updated_at"}


class PositionError(ValueError):
    """持仓业务错误（参数/账本一致性），由接口层转成结构化失败。"""


@dataclass
class LedgerState:
    quantity: float
    average_cost: float
    realized_profit: float


def compute_ledger(trades: Iterable[Any]) -> LedgerState:
    """按**移动加权平均成本**逐笔推导持仓状态（纯函数，便于单测）。

    - 买入：数量累加、成本累加；
    - 卖出：按当时均价结转成本，差额计入已实现盈亏；
    - ``调整``：只改数量（按当时均价平移成本），不产生已实现盈亏。

    与旧实现的区别：旧版用「全部买入总额 / 全部买入数量」做均价，清仓后再买入
    （10 元买、20 元卖、30 元再买）会算出 20 元成本而不是 30 元。
    """
    hold_qty = 0.0
    hold_cost = 0.0
    realized = 0.0
    for item in trades:
        qty = float(getattr(item, "quantity", 0) or 0)
        price = float(getattr(item, "price", 0) or 0)
        trade_type = getattr(item, "type", "")
        if trade_type == BUY:
            hold_qty += qty
            hold_cost += qty * price
        elif trade_type == ADJUST:
            if hold_qty + qty <= 0:
                hold_qty = 0.0
                hold_cost = 0.0
            else:
                average = hold_cost / hold_qty if hold_qty else 0.0
                hold_qty += qty
                hold_cost = average * hold_qty
        else:
            sell_qty = min(qty, hold_qty)
            average = hold_cost / hold_qty if hold_qty else 0.0
            realized += sell_qty * (price - average)
            hold_qty -= sell_qty
            hold_cost -= sell_qty * average
    average_cost = hold_cost / hold_qty if hold_qty > 0 else 0.0
    return LedgerState(quantity=hold_qty, average_cost=average_cost, realized_profit=realized)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


class PositionManager:
    def __init__(self, data_loader=None):
        self.data_loader = data_loader
        self.db = QuestDBClient()
        self.engine = self.db.pd_conn
        self.Session = sessionmaker(bind=self.engine)
        self._last_refresh: dict[str, float] = {}
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ 工具
    def _get_mode_instance(self, mode_name, params_str):
        params = json.loads(params_str) if params_str else {}
        return create_mode(mode_name, params)

    def _update(self, sql: str, params: Iterable[Any]) -> bool:
        """参数化 UPDATE；失败返回 False 并记录日志。"""
        try:
            self.db.execute(sql, tuple(params))
            return True
        except Exception as exc:
            logger.error("持仓更新失败: %s | SQL=%s", exc, sql[:200])
            return False

    @staticmethod
    def _clean_updates(updates: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        """过滤非法列名与 NaN（NaN 写进 DOUBLE 会变成非预期值）。"""
        cleaned: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                logger.warning("忽略不可更新的列: %s", key)
                continue
            if isinstance(value, float) and math.isnan(value):
                continue
            if value is None and key not in ("first_entry_date",):
                continue
            cleaned[key] = value
        return cleaned

    # ------------------------------------------------------------------ 组合
    def create_profolio(self, name, cash=0.0, force=False):
        session = self.Session()
        try:
            if not force:
                existing = (
                    session.query(PortfolioModel)
                    .filter_by(name=name, deleted_at=None)
                    .first()
                )
                if existing:
                    logger.info("组合 %s 已存在，返回 %s", name, existing.portfolio_id)
                    return existing.portfolio_id

            portfolio_id = str(uuid.uuid4())[:8]
            now = now_cn()
            session.add(
                PortfolioModel(
                    portfolio_id=portfolio_id,
                    name=name,
                    total_assets=cash,
                    cash=cash,
                    market_value=0.0,
                    unrealized_profit=0.0,
                    realized_profit=0.0,
                    id=now,
                )
            )
            session.commit()
            logger.info("已创建组合: %s (%s)", name, portfolio_id)
            return portfolio_id
        except Exception as exc:
            session.rollback()
            logger.error("创建组合失败: %s", exc)
            return None
        finally:
            session.close()

    def modify_profolio(self, portfolio_id, updates):
        cleaned = self._clean_updates(updates, _PORTFOLIO_UPDATABLE)
        if not cleaned:
            return True
        set_clause = ", ".join(f'"{key}" = %s' for key in cleaned)
        sql = f"UPDATE portfolios SET {set_clause} WHERE portfolio_id = %s"
        params = list(cleaned.values()) + [portfolio_id]
        ok = self._update(sql, params)
        self._last_refresh.pop(portfolio_id, None)
        return ok

    def calculate_profolio(self, portfolio_id):
        session = self.Session()
        try:
            latest_pro = (
                session.query(PortfolioModel)
                .filter_by(portfolio_id=portfolio_id, deleted_at=None)
                .order_by(desc(PortfolioModel.id))
                .first()
            )
            if not latest_pro:
                return False
            total_cash = _safe_float(latest_pro.cash) or 0.0

            pos_ids = {
                row[0]
                for row in session.query(PositionModel.pos_id)
                .filter_by(portfolio_id=portfolio_id, deleted_at=None)
                .all()
            }
            total_market_value = total_unrealized = total_realized = 0.0
            for pos_id in pos_ids:
                latest_pos = (
                    session.query(PositionModel)
                    .filter_by(pos_id=pos_id, deleted_at=None)
                    .order_by(desc(PositionModel.id))
                    .first()
                )
                if latest_pos:
                    total_market_value += _safe_float(latest_pos.market_value) or 0.0
                    total_unrealized += _safe_float(latest_pos.unrealized_profit) or 0.0
                    total_realized += _safe_float(latest_pos.realized_profit) or 0.0
        finally:
            session.close()

        return self.modify_profolio(
            portfolio_id,
            {
                "market_value": total_market_value,
                "unrealized_profit": total_unrealized,
                "realized_profit": total_realized,
                "total_assets": total_cash + total_market_value,
            },
        )

    def list__profolio(self):
        session = self.Session()
        try:
            records = (
                session.query(PortfolioModel)
                .filter_by(deleted_at=None)
                .order_by(desc(PortfolioModel.id))
                .all()
            )
            seen: set[str] = set()
            latest = []
            for record in records:  # 已按 id 倒序：每个 portfolio_id 取最新一条
                if record.portfolio_id in seen:
                    continue
                seen.add(record.portfolio_id)
                latest.append(record)
            return latest
        finally:
            session.close()

    def delete_profolio(self, portfolio_id):
        try:
            now = now_cn()
            session = self.Session()
            try:
                pos_ids = {
                    row[0]
                    for row in session.query(PositionModel.pos_id)
                    .filter_by(portfolio_id=portfolio_id, deleted_at=None)
                    .all()
                }
            finally:
                session.close()

            results = []
            for pos_id in pos_ids:
                results.append(
                    self._update(
                        "UPDATE trade_records SET deleted_at = %s "
                        "WHERE pos_id = %s AND deleted_at IS NULL",
                        (now, pos_id),
                    )
                )
            results.append(
                self._update(
                    "UPDATE positions SET deleted_at = %s "
                    "WHERE portfolio_id = %s AND deleted_at IS NULL",
                    (now, portfolio_id),
                )
            )
            results.append(
                self._update(
                    "UPDATE portfolios SET deleted_at = %s "
                    "WHERE portfolio_id = %s AND deleted_at IS NULL",
                    (now, portfolio_id),
                )
            )
            if not all(results):
                logger.error("删除组合 %s 部分更新失败 %s", portfolio_id, results)
                return False
            self._last_refresh.pop(portfolio_id, None)
            logger.info("已软删除组合: %s", portfolio_id)
            return True
        except Exception as exc:
            logger.error("删除组合失败: %s", exc)
            return False

    # ------------------------------------------------------------------ 仓位
    def get_position_by_ticker(self, portfolio_id, ticker):
        ticker = normalize_code(ticker)
        for position in self.list_positions(portfolio_id):
            if normalize_code(position.ticker) == ticker:
                return position
        return None

    def list_positions(self, portfolio_id):
        session = self.Session()
        try:
            records = (
                session.query(PositionModel)
                .filter_by(portfolio_id=portfolio_id, deleted_at=None)
                .order_by(desc(PositionModel.id))
                .all()
            )
            seen: set[str] = set()
            latest = []
            for record in records:  # 倒序后取第一条 = 最新快照（与 calculate_profit 一致）
                if record.pos_id in seen:
                    continue
                seen.add(record.pos_id)
                record.mode_instance = self._get_mode_instance(record.mode, record.mode_params)
                latest.append(record)
            return latest
        finally:
            session.close()

    def create_position(self, portfolio_id, ticker, name, mode, mode_params=None):
        session = self.Session()
        try:
            pos_id = str(uuid.uuid4())[:8]
            now = now_cn()
            session.add(
                PositionModel(
                    pos_id=pos_id,
                    portfolio_id=portfolio_id,
                    ticker=normalize_code(ticker),
                    name=name,
                    mode=mode,
                    created_at=now,
                    updated_at=now,
                    id=now,
                    quantity=0.0,
                    mode_params=json.dumps(mode_params or {}),
                )
            )
            session.commit()
            logger.info("已创建仓位: %s (%s) @ %s", ticker, pos_id, portfolio_id)
            return pos_id
        except Exception as exc:
            session.rollback()
            logger.error("创建仓位失败: %s", exc)
            return None
        finally:
            session.close()

    def modify_position(self, pos_id, updates, *, recalculate: bool = True):
        session = self.Session()
        try:
            position = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id, deleted_at=None)
                .order_by(desc(PositionModel.id))
                .first()
            )
            if not position:
                return False
            pro_id = position.portfolio_id
            existing_mode_params = json.loads(position.mode_params or "{}")

            payload = dict(updates)
            if isinstance(payload.get("mode_params"), dict):
                # 合并而非整体替换（保留 stop_price / pyramid_level / grid_level）
                payload["mode_params"] = json.dumps(
                    {**existing_mode_params, **payload["mode_params"]}
                )

            cleaned = self._clean_updates(payload, _POSITION_UPDATABLE)
            if not cleaned:
                return True
            set_clause = ", ".join(f'"{key}" = %s' for key in cleaned)
            sql = f"UPDATE positions SET {set_clause} WHERE pos_id = %s"
            ok = self._update(sql, list(cleaned.values()) + [pos_id])
            if ok and recalculate:
                self.calculate_profolio(pro_id)
            return ok
        finally:
            session.close()

    def delete_position(self, pos_id, history_id=None):
        try:
            now = now_cn()
            if history_id:
                ok = self._update(
                    "UPDATE positions SET deleted_at = %s WHERE pos_id = %s AND id = %s",
                    (now, pos_id, history_id),
                )
            else:
                ok = self._update(
                    "UPDATE trade_records SET deleted_at = %s "
                    "WHERE pos_id = %s AND deleted_at IS NULL",
                    (now, pos_id),
                )
                ok = (
                    self._update(
                        "UPDATE positions SET deleted_at = %s "
                        "WHERE pos_id = %s AND deleted_at IS NULL",
                        (now, pos_id),
                    )
                    and ok
                )
            return bool(ok)
        except Exception as exc:
            logger.error("删除仓位失败: %s", exc)
            return False

    # ------------------------------------------------------------------ 流水
    def list_trade_record(self, pos_id):
        session = self.Session()
        try:
            return (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id, deleted_at=None)
                .order_by(TradeRecordModel.trade_date)
                .all()
            )
        finally:
            session.close()

    @staticmethod
    def _net_quantity(trades: Iterable[TradeRecordModel]) -> float:
        quantity = 0.0
        for trade in trades:
            if trade.type == BUY:
                quantity += float(trade.quantity or 0)
            elif trade.type == ADJUST:
                quantity += float(trade.quantity or 0)  # 调整量自带正负
            else:
                quantity -= float(trade.quantity or 0)
        return quantity

    def add_trade_record(self, pos_id, trade_type, quantity, price, trade_date=None):
        quantity = _safe_float(quantity)
        price = _safe_float(price)
        if quantity is None or quantity <= 0:
            raise PositionError("成交数量必须为正数")
        if price is None or price <= 0:
            raise PositionError("成交价格必须为正数")
        if trade_type not in (BUY, SELL):
            raise PositionError(f"不支持的交易类型: {trade_type}")

        with self._write_lock:
            session = self.Session()
            try:
                existing = (
                    session.query(TradeRecordModel)
                    .filter_by(pos_id=pos_id, deleted_at=None)
                    .order_by(TradeRecordModel.trade_date)
                    .all()
                )
                if trade_type == SELL:
                    held = self._net_quantity(existing)
                    if quantity > held + 1e-9:
                        raise PositionError(
                            f"卖出数量 {quantity} 超过当前持仓 {held:.4f}，请先核对持仓"
                        )
                is_first_trade = len(existing) == 0
                had_position = self._net_quantity(existing) > 0

                trade_id = str(uuid.uuid4())[:8]
                now = now_cn()
                actual_trade_date = trade_date or now
                session.add(
                    TradeRecordModel(
                        trade_id=trade_id,
                        pos_id=pos_id,
                        type=trade_type,
                        quantity=quantity,
                        price=price,
                        trade_date=actual_trade_date,
                        updated_at=now,
                    )
                )
                session.commit()
                session.close()

                if is_first_trade and trade_type == BUY:
                    self._update_first_entry_date(pos_id, actual_trade_date)

                self.calculate_profit(
                    pos_id,
                    trade={
                        "type": trade_type,
                        "price": price,
                        "quantity": quantity,
                        "had_position": had_position,
                    },
                )
                return trade_id
            except PositionError:
                raise
            except Exception as exc:
                session.rollback()
                logger.error("写入成交失败: %s", exc)
                return None
            finally:
                session.close()

    def adjust_quantity(self, pos_id, target_quantity: float, price: float | None = None):
        """把持仓数量调整到目标值：写入一条 ``调整`` 流水。

        旧实现直接改 ``positions.quantity``，下一次 ``calculate_profit`` 会按流水
        重算把手工修改抹掉；改成流水后既不会被覆盖，也能被审计。
        """
        target = _safe_float(target_quantity)
        if target is None or target < 0:
            raise PositionError("目标持仓数量必须是非负数")

        current = self._net_quantity(self.list_trade_record(pos_id))
        delta = round(target - current, 8)
        if abs(delta) < 1e-9:
            return True

        session = self.Session()
        try:
            latest = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id, deleted_at=None)
                .order_by(desc(PositionModel.id))
                .first()
            )
            if not latest:
                raise PositionError(f"未找到仓位 {pos_id}")
            reference_price = (
                _safe_float(price) or _safe_float(latest.cost) or _safe_float(latest.market_value) or 0.0
            )
        finally:
            session.close()
        if reference_price <= 0:
            raise PositionError("无法确定调整价格，请先有一笔真实成交或显式传入 price")

        with self._write_lock:
            session = self.Session()
            try:
                now = now_cn()
                session.add(
                    TradeRecordModel(
                        trade_id=str(uuid.uuid4())[:8],
                        pos_id=pos_id,
                        type=ADJUST,
                        quantity=delta,
                        price=reference_price,
                        trade_date=now,
                        updated_at=now,
                    )
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.error("写入调整流水失败: %s", exc)
                return False
            finally:
                session.close()
        self.calculate_profit(pos_id)
        logger.info("仓位 %s 数量调整为 %s（delta=%s）", pos_id, target, delta)
        return True

    def modify_trade_record(self, trade_id, updates):
        cleaned = self._clean_updates(updates, _TRADE_UPDATABLE)
        if not cleaned:
            return True
        session = self.Session()
        try:
            trade = session.query(TradeRecordModel).filter_by(trade_id=trade_id).first()
            if not trade:
                return False
            pos_id = trade.pos_id
        finally:
            session.close()
        set_clause = ", ".join(f'"{key}" = %s' for key in cleaned)
        ok = self._update(
            f"UPDATE trade_records SET {set_clause} WHERE trade_id = %s",
            list(cleaned.values()) + [trade_id],
        )
        if ok:
            self.calculate_profit(pos_id)
        return ok

    def delete_trade_record(self, trade_id):
        session = self.Session()
        try:
            trade = session.query(TradeRecordModel).filter_by(trade_id=trade_id).first()
            if not trade:
                return False
            pos_id = trade.pos_id
        finally:
            session.close()
        ok = self._update(
            "UPDATE trade_records SET deleted_at = %s WHERE trade_id = %s",
            (now_cn(), trade_id),
        )
        if ok:
            self.calculate_profit(pos_id)
        return ok

    def _update_first_entry_date(self, pos_id, first_entry_date):
        """首笔买入时记录建仓日期（用于「建仓日 ATR」与时间止损）。"""
        try:
            ok = self._update(
                "UPDATE positions SET first_entry_date = %s WHERE pos_id = %s",
                (first_entry_date, pos_id),
            )
            if ok:
                logger.info("已记录 %s 的建仓日期: %s", pos_id, first_entry_date)
            return ok
        except Exception as exc:
            logger.error("记录建仓日期失败: %s", exc)
            return False

    # ------------------------------------------------------------------ 盈亏
    def _fetch_price_frames(self, tickers: list[str], start_date: str) -> dict[str, Any]:
        if not self.data_loader or not tickers:
            return {}
        try:
            return self.data_loader.ohlc(
                tickers, start_date, now_cn().strftime("%Y-%m-%d"), freq="d",
                now=True, adjust="qfq",
            )
        except Exception as exc:
            logger.warning("批量拉取行情失败 %s: %s", tickers, exc)
            return {}

    def refresh_all_positions(self, portfolio_id, force=False):
        """刷新组合内全部仓位的现价/盈亏（一次批量行情 + N 次 UPDATE）。"""
        import time as _time

        now = _time.time()
        if not force and (now - self._last_refresh.get(portfolio_id, 0)) < 60:
            return True

        positions = self.list_positions(portfolio_id)
        if not positions:
            self.calculate_profolio(portfolio_id)
            self._last_refresh[portfolio_id] = now
            return True

        start = now_cn() - timedelta(days=90)
        for position in positions:
            entry = getattr(position, "first_entry_date", None)
            if isinstance(entry, datetime):
                start = min(start, entry - timedelta(days=60))
        frames = self._fetch_price_frames(
            [p.ticker for p in positions if p.ticker], start.strftime("%Y-%m-%d")
        )
        for position in positions:
            self.calculate_profit(
                position.pos_id,
                price_frame=frames.get(normalize_code(position.ticker)),
                recalculate=False,
            )
        self.calculate_profolio(portfolio_id)
        self._last_refresh[portfolio_id] = now
        return True

    def get_portfolio_summary(self, portfolio_id):
        self.refresh_all_positions(portfolio_id)

        session = self.Session()
        try:
            latest_pro = (
                session.query(PortfolioModel)
                .filter_by(portfolio_id=portfolio_id, deleted_at=None)
                .order_by(desc(PortfolioModel.id))
                .first()
            )
            if not latest_pro:
                return {"error": "Portfolio not found"}
            positions = self.list_positions(portfolio_id)

            pos_list = []
            for position in positions:
                mode_params = json.loads(position.mode_params or "{}")
                quantity = _safe_float(position.quantity) or 0.0
                market_value = _safe_float(position.market_value) or 0.0
                pos_list.append(
                    {
                        "pos_id": position.pos_id,
                        "ticker": position.ticker,
                        "name": position.name,
                        "quantity": quantity,
                        "cost_price": _safe_float(position.cost) or 0.0,
                        "current_price": round(market_value / quantity, 4) if quantity else 0.0,
                        "market_value": round(market_value, 2),
                        "unrealized_pnl": round(_safe_float(position.unrealized_profit) or 0.0, 2),
                        "realized_pnl": round(_safe_float(position.realized_profit) or 0.0, 2),
                        "return_rate": f"{(_safe_float(position.unrealized_profit_rate) or 0.0) * 100:.2f}%",
                        "mode": position.mode,
                        "stop_price": _safe_float(position.stop_price),
                        "initial_atr": mode_params.get("initial_atr"),
                        "n_factor": mode_params.get("n_factor"),
                        "risk_per_trade": mode_params.get("risk_per_trade"),
                        "time_stop_days": mode_params.get("time_stop_days"),
                        "pyramid_level": mode_params.get("pyramid_level", 0),
                        "first_entry_date": position.first_entry_date.isoformat()
                        if position.first_entry_date
                        else None,
                        "mode_params": mode_params,
                    }
                )

            return {
                "portfolio_name": latest_pro.name,
                "total_assets": round(_safe_float(latest_pro.total_assets) or 0.0, 2),
                "cash": round(_safe_float(latest_pro.cash) or 0.0, 2),
                "market_value": round(_safe_float(latest_pro.market_value) or 0.0, 2),
                "total_unrealized_pnl": round(_safe_float(latest_pro.unrealized_profit) or 0.0, 2),
                "positions": pos_list,
            }
        finally:
            session.close()

    def calculate_profit(self, pos_id, trade=None, price_frame=None, recalculate: bool = True):
        session = self.Session()
        try:
            trades = (
                session.query(TradeRecordModel)
                .filter_by(pos_id=pos_id, deleted_at=None)
                .order_by(TradeRecordModel.trade_date)
                .all()
            )
            latest_pos = (
                session.query(PositionModel)
                .filter_by(pos_id=pos_id, deleted_at=None)
                .order_by(desc(PositionModel.id))
                .first()
            )
            if not latest_pos:
                return False
            position_state = {
                "ticker": latest_pos.ticker,
                "mode": latest_pos.mode,
                "mode_params": latest_pos.mode_params,
                "first_entry_date": latest_pos.first_entry_date,
            }
        finally:
            session.close()

        # ---- 移动加权平均成本：支持部分卖出 / 清仓后再买入 ----
        ledger = compute_ledger(trades)
        hold_qty = ledger.quantity
        average_cost = ledger.average_cost
        realized = ledger.realized_profit

        # ---- 行情（一次取，前复权序列；不复权价单独留一列）----
        frame = price_frame
        if frame is None:
            frame = self._fetch_price_frames(
                [position_state["ticker"]],
                (now_cn() - timedelta(days=90)).strftime("%Y-%m-%d"),
            ).get(normalize_code(position_state["ticker"]))
        raw_price = None
        if frame is not None and not frame.empty:
            if "raw_close" in frame.columns:
                raw_price = _safe_float(frame["raw_close"].iloc[-1])
            if raw_price is None:
                raw_price = _safe_float(frame["close"].iloc[-1])
        if raw_price is None:
            raw_price = float(trades[-1].price) if trades else average_cost

        market_value = hold_qty * raw_price
        unrealized = (raw_price - average_cost) * hold_qty if hold_qty > 0 else 0.0
        return_rate = (raw_price / average_cost - 1) if average_cost > 0 else 0.0

        result = self.modify_position(
            pos_id,
            {
                "cost": average_cost,
                "quantity": hold_qty,
                "market_value": market_value,
                "unrealized_profit": unrealized,
                "unrealized_profit_rate": return_rate,
                "realized_profit": realized,
                "total_profit": unrealized + realized,
            },
            recalculate=recalculate,
        )

        if result and frame is not None and not frame.empty and position_state["ticker"]:
            self._refresh_stop_price(pos_id, position_state, trades, frame, trade)
        return result

    def _refresh_stop_price(self, pos_id, state, trades, frame, trade) -> None:
        """按模式计算止损价与 initial_atr（失败只告警，不影响盈亏落库）。"""
        try:
            mode_params = json.loads(state["mode_params"] or "{}")
            mode = create_mode(state["mode"], mode_params)
            if trade:
                mode.advance_level(
                    trade.get("type"),
                    trade.get("price"),
                    trade.get("quantity"),
                    trade.get("had_position", False),
                )
                mode_params.update(mode.get_mode_data())

            current_price = _safe_float(frame["close"].iloc[-1])
            entry_price = None
            for item in trades:
                if item.type == BUY:
                    entry_price = float(item.price)
                    break
            if entry_price is None:
                entry_price = current_price

            entry_ts = state.get("first_entry_date")
            if entry_ts is not None and not mode_params.get("initial_atr"):
                hist = frame[frame.index <= pd.Timestamp(entry_ts)]
                if len(hist) >= 15 and hasattr(mode, "_calculate_atr_from_ohlcv"):
                    atr_value = _safe_float(mode._calculate_atr_from_ohlcv(hist))
                    if atr_value and atr_value > 0:
                        mode_params["initial_atr"] = atr_value
                        mode.initial_atr = atr_value
                        logger.info("%s 用建仓日数据得到 initial_atr=%.4f", pos_id, atr_value)

            stop_price = mode.calculate_stop_price(
                {"current_price": current_price, "entry_price": entry_price, "ohlcv": frame}
            )
            self.save_stop_price(pos_id, stop_price, mode_params)
        except Exception as exc:
            logger.warning("更新止损价失败 %s: %s", pos_id, exc)

    def save_stop_price(self, pos_id, stop_price, mode_params=None):
        stop = _safe_float(stop_price)
        if stop is None or stop <= 0:
            logger.warning("%s 止损价无效（%s），跳过落库", pos_id, stop_price)
            return False
        session = self.Session()
        try:
            if mode_params is None:
                position = (
                    session.query(PositionModel)
                    .filter_by(pos_id=pos_id, deleted_at=None)
                    .order_by(desc(PositionModel.id))
                    .first()
                )
                if not position:
                    return False
                mode_params = json.loads(position.mode_params or "{}")
        finally:
            session.close()

        merged = dict(mode_params)
        merged["stop_price"] = stop
        return self._update(
            "UPDATE positions SET stop_price = %s, mode_params = %s WHERE pos_id = %s",
            (stop, json.dumps(merged), pos_id),
        )
