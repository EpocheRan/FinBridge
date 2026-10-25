"""面向 LLM 的持仓接口层。

2026-10 修订要点：

- ``action`` 做**白名单**校验：以前任何非 buy 的值（包括技能文档里写的
  ``open``/``close``/拼错的 ``sel``）都会被当作「卖出」记账。
- 卖出前校验持仓数量，禁止出现负持仓和凭空增加的现金。
- 写操作与资金更新分离检查：资金更新失败会把刚写入的流水软删除并返回失败。
- 信号计算使用**真实** ``pyramid_level`` / ``has_added`` / 建仓价，
  不再硬编码 0，避免加仓信号反复触发、时间止损误判。
- 失败响应统一带 ``error_details``（与行情/财务工具的错误信封对齐）。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from data_interface.common.security_code import normalize_code
from data_interface.common.timeutil import now_cn

from .manager import BUY, SELL, PositionError, PositionManager

logger = logging.getLogger("PositionLLMTool")

_ACTION_BUY = {"buy", "b", "买入", "买", "加仓"}
_ACTION_SELL = {"sell", "s", "卖出", "卖", "close", "平仓", "清仓", "减仓"}


class PositionAgentInterface:
    """Interface for Large Language Models to interact with the Position Management System."""

    def __init__(self, data_loader=None):
        self.manager = PositionManager(data_loader=data_loader)

    # ------------------------------------------------------------------ 响应
    @staticmethod
    def _response(success: bool, message: str, data: dict | None = None, error_type: str | None = None):
        response: dict = {"success": success, "message": message, "data": data or {}}
        if not success:
            response["error_details"] = {
                "type": error_type or "PositionError",
                "suggestion": "请检查参数（组合 ID / 代码 / 数量 / 价格）后重试。",
            }
        return response

    def _fail(self, message: str, error_type: str = "PositionError"):
        return self._response(False, message, error_type=error_type)

    # ------------------------------------------------------------------ 组合
    def create_portfolio(
        self,
        name: str,
        initial_cash: float,
        reuse_existing: bool = None,
        tickers: list = None,
    ):
        try:
            portfolios = self.manager.list__profolio()
            name_norm = (name or "").strip().lower()
            tickers_set = {normalize_code(t) for t in (tickers or []) if str(t).strip()}

            candidates = []
            for portfolio in portfolios:
                reasons = []
                if portfolio.name and portfolio.name.strip().lower() == name_norm:
                    reasons.append("same_name")
                if tickers_set:
                    held = {
                        normalize_code(position.ticker)
                        for position in self.manager.list_positions(portfolio.portfolio_id)
                        if position.ticker
                    }
                    if held & tickers_set:
                        reasons.append("same_ticker")
                if reasons:
                    candidates.append(
                        {
                            "portfolio_id": portfolio.portfolio_id,
                            "name": portfolio.name,
                            "cash": portfolio.cash,
                            "total_assets": portfolio.total_assets,
                            "reason": reasons,
                        }
                    )

            if candidates and reuse_existing is None:
                first = candidates[0]
                reasons_cn = "/".join(
                    "同名" if item == "same_name" else "同标的" for item in first["reason"]
                )
                return self._response(
                    False,
                    f"发现相似组合「{first['name']}」({first['portfolio_id']})，原因：{reasons_cn}。"
                    f"请确认：复用现有组合请再次调用并传 reuse_existing=true；"
                    f"要新建则传 reuse_existing=false。",
                    {"needs_confirmation": True, "candidates": candidates},
                    error_type="NeedConfirmation",
                )

            if candidates and reuse_existing is True:
                first = candidates[0]
                return self._response(
                    True,
                    f"复用现有组合「{first['name']}」({first['portfolio_id']})。",
                    {
                        "portfolio_id": first["portfolio_id"],
                        "is_new": False,
                        "reused": True,
                        "candidates": candidates,
                    },
                )

            portfolio_id = self.manager.create_profolio(
                name, initial_cash, force=(reuse_existing is False)
            )
            if portfolio_id:
                return self._response(
                    True,
                    f"Portfolio '{name}' created successfully.",
                    {"portfolio_id": portfolio_id, "is_new": True},
                )
            return self._fail("创建组合失败，请查看服务日志。", "DatabaseError")
        except Exception as exc:
            return self._fail(f"创建组合异常: {exc}", type(exc).__name__)

    def list_portfolios(self):
        try:
            portfolios = self.manager.list__profolio()
            data = [
                {
                    "portfolio_id": item.portfolio_id,
                    "name": item.name,
                    "cash": item.cash,
                    "market_value": item.market_value,
                    "total_assets": item.total_assets,
                    "realized_profit": item.realized_profit,
                    "unrealized_profit": item.unrealized_profit,
                }
                for item in portfolios
            ]
            return self._response(True, f"Found {len(data)} portfolios.", {"portfolios": data})
        except Exception as exc:
            return self._fail(f"列出组合异常: {exc}", type(exc).__name__)

    def delete_portfolio(self, portfolio_id: str):
        try:
            if self.manager.delete_profolio(portfolio_id):
                return self._response(True, f"Portfolio {portfolio_id} deleted.")
            return self._fail(f"删除组合 {portfolio_id} 失败（部分数据可能未更新）", "DatabaseError")
        except Exception as exc:
            return self._fail(f"删除组合异常: {exc}", type(exc).__name__)

    def modify_portfolio(self, portfolio_id: str, name: str = None, cash: float = None):
        try:
            updates: dict = {}
            if name is not None:
                updates["name"] = name
            if cash is not None:
                updates["cash"] = float(cash)
            if not updates:
                return self._fail("没有提供要修改的字段。", "InvalidArgument")
            if self.manager.modify_profolio(portfolio_id, updates):
                return self._response(True, f"Portfolio {portfolio_id} updated.", updates)
            return self._fail(f"更新组合 {portfolio_id} 失败", "DatabaseError")
        except Exception as exc:
            return self._fail(f"更新组合异常: {exc}", type(exc).__name__)

    # ------------------------------------------------------------------ 仓位
    def _resolve_pos_id(self, pos_id: str) -> str:
        """支持 8 位短 ID 与完整 UUID 前缀。"""
        text = str(pos_id or "").strip()
        if len(text) <= 8:
            return text
        from sqlalchemy import text as sql_text

        pattern = text[:8] + "%"
        with self.manager.db.pd_conn.connect() as connection:
            row = connection.execute(
                sql_text(
                    "SELECT pos_id FROM positions WHERE pos_id LIKE :pattern "
                    "AND deleted_at IS NULL LIMIT 1"
                ),
                {"pattern": pattern},
            ).fetchone()
        if not row:
            raise PositionError(f"未找到仓位: {pos_id}")
        return row[0]

    def modify_position(
        self,
        pos_id: str,
        name: str = None,
        quantity: float = None,
        mode_params: dict = None,
    ):
        try:
            short_id = self._resolve_pos_id(pos_id)
            updates: dict = {}
            if name is not None:
                updates["name"] = name
            if mode_params:
                updates["mode_params"] = mode_params

            changed = False
            if updates:
                changed = bool(self.manager.modify_position(short_id, updates)) or changed
            if quantity is not None:
                # 数量必须走「调整流水」，否则下一次盈亏重算会把手工值抹掉
                changed = bool(self.manager.adjust_quantity(short_id, float(quantity))) or changed
            if not changed:
                return self._fail("没有产生任何修改。", "InvalidArgument")
            return self._response(
                True,
                f"Position {short_id} updated.",
                {"pos_id": short_id, "updates": updates, "quantity": quantity},
            )
        except PositionError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            return self._fail(f"修改仓位异常: {exc}", type(exc).__name__)

    def get_portfolio_status(self, portfolio_id: str):
        try:
            summary = self.manager.get_portfolio_summary(portfolio_id)
            if "error" in summary:
                return self._fail(summary["error"], "NotFound")
            return self._response(True, "Portfolio status retrieved.", summary)
        except Exception as exc:
            return self._fail(f"读取组合状态异常: {exc}", type(exc).__name__)

    def get_available_modes(self):
        modes = [
            {
                "name": "ATR",
                "description": "基于平均真实波幅 (ATR) 的动态风控模式。包含追踪止损、加仓信号检测以及基于风险百分比的头寸计算。",
                "parameters": {
                    "initial_atr": "初始 ATR 值（可选，若不提供则自动从历史数据计算）",
                    "n_factor": "ATR 系数，通常为 2.0，用于确定止损距离",
                    "risk_per_trade": "单笔交易风险比例，例如 0.02 表示账户资金的 2%",
                    "time_stop_days": "僵尸仓位自动清理天数",
                },
            },
            {
                "name": "GRID",
                "description": "网格交易模式：以中枢价为核心上下铺设网格，下跌逐格买入、上涨逐格卖出，跌破下界清仓。",
                "parameters": {
                    "base_price": "网格中枢价（可选，不填则以当前价为准）",
                    "grid_step": "网格间距（绝对价格，优先使用）",
                    "grid_step_pct": "网格间距（百分比，例如 0.02 表示 2%）",
                    "grid_count": "上下各铺设几格（默认 5）",
                    "quantity_per_grid": "每格交易数量",
                    "max_position": "最大持仓数量（0 表示不限）",
                    "lower_bound": "网格下界（0 表示自动）",
                    "upper_bound": "网格上界（0 表示自动）",
                },
            },
        ]
        return self._response(True, "Available modes retrieved.", {"modes": modes})

    def open_position(
        self, portfolio_id: str, ticker: str, name: str = None, mode: str = "ATR", **kwargs
    ):
        ticker = normalize_code(ticker)
        if not ticker:
            return self._fail("股票代码不能为空。", "InvalidArgument")
        existing = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if existing:
            return self._fail(
                f"已存在 {ticker} 的仓位（pos_id={existing.pos_id}），请用 pm_execute_trade 交易。",
                "DuplicatePosition",
            )
        try:
            pos_id = self.manager.create_position(
                portfolio_id, ticker, name or ticker, (mode or "ATR").upper(), kwargs
            )
            if pos_id:
                return self._response(
                    True, f"Position tracking started for {ticker}.", {"pos_id": pos_id}
                )
            return self._fail("创建仓位失败，请查看服务日志。", "DatabaseError")
        except Exception as exc:
            return self._fail(f"创建仓位异常: {exc}", type(exc).__name__)

    # ------------------------------------------------------------------ 交易
    @staticmethod
    def _normalize_action(action: str) -> str:
        key = str(action or "").strip().lower()
        if key in _ACTION_BUY:
            return BUY
        if key in _ACTION_SELL:
            return SELL
        raise PositionError(
            f"不支持的 action: {action!r}。买入用 buy/买入，卖出用 sell/卖出/close，"
            f"开仓请用 pm_open_position。"
        )

    def execute_trade(
        self, portfolio_id: str, ticker: str, action: str, quantity: float, price: float
    ):
        ticker = normalize_code(ticker)
        try:
            trade_type = self._normalize_action(action)
            quantity = float(quantity)
            price = float(price)
        except PositionError as exc:
            return self._fail(str(exc), "InvalidAction")
        except (TypeError, ValueError):
            return self._fail("数量或价格不是数字。", "InvalidArgument")

        if quantity <= 0 or price <= 0:
            return self._fail("数量与价格必须为正数。", "InvalidArgument")

        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            if trade_type != BUY:
                return self._fail(f"卖出失败：找不到 {ticker} 的持仓。", "PositionNotFound")
            created = self.open_position(portfolio_id, ticker, ticker, "ATR")
            if not created["success"]:
                return created
            position = self.manager.get_position_by_ticker(portfolio_id, ticker)
            if not position:
                return self._fail(f"无法为 {ticker} 创建仓位。", "DatabaseError")

        amount = quantity * price
        summary = self.manager.get_portfolio_summary(portfolio_id)
        if "error" in summary:
            return self._fail(summary["error"], "NotFound")
        current_cash = float(summary.get("cash") or 0.0)

        if trade_type == BUY:
            if current_cash + 1e-6 < amount:
                return self._fail(
                    f"现金不足：可用 {current_cash:.2f}，需要 {amount:.2f}", "InsufficientCash"
                )
            cash_change = -amount
        else:
            cash_change = amount

        try:
            trade_id = self.manager.add_trade_record(
                position.pos_id, trade_type, quantity, price
            )
        except PositionError as exc:
            return self._fail(str(exc), "LedgerError")
        except Exception as exc:
            return self._fail(f"记录成交异常: {exc}", type(exc).__name__)

        if not trade_id:
            return self._fail("记录成交失败，请查看服务日志。", "DatabaseError")

        new_cash = round(current_cash + cash_change, 4)
        if not self.manager.modify_profolio(portfolio_id, {"cash": new_cash}):
            # 资金没更新成功：回滚刚写入的流水，避免账实不符
            self.manager.delete_trade_record(trade_id)
            return self._fail("成交已回滚：资金余额更新失败。", "DatabaseError")

        self.manager.calculate_profolio(portfolio_id)
        updated = self.manager.get_position_by_ticker(portfolio_id, ticker)
        return self._response(
            True,
            f"Trade recorded: {trade_type} {quantity} {ticker} @ {price}.",
            {
                "trade_id": trade_id,
                "new_quantity": updated.quantity if updated else None,
                "new_cost": updated.cost if updated else None,
                "unrealized_pnl": updated.unrealized_profit if updated else None,
                "cash_balance": new_cash,
            },
        )

    def close_position(self, portfolio_id: str, ticker: str, price: float = None):
        ticker = normalize_code(ticker)
        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            return self._fail(f"找不到 {ticker} 的持仓。", "PositionNotFound")

        held = self.manager._net_quantity(self.manager.list_trade_record(position.pos_id))
        if held <= 0:
            return self._fail(f"{ticker} 当前持仓为 0，无需平仓。", "InvalidArgument")

        if price is None:
            return self._fail("平仓需要提供成交价格 price。", "InvalidArgument")
        return self.execute_trade(portfolio_id, ticker, SELL, held, price)

    # ------------------------------------------------------------------ 信号
    def get_trading_signals(self, portfolio_id: str, ticker: str):
        ticker = normalize_code(ticker)
        position = self.manager.get_position_by_ticker(portfolio_id, ticker)
        if not position:
            return self._fail(f"找不到 {ticker} 的持仓。", "PositionNotFound")

        try:
            end_str = now_cn().strftime("%Y-%m-%d")
            start_str = (now_cn() - timedelta(days=90)).strftime("%Y-%m-%d")
            ohlcv = None
            if self.manager.data_loader:
                data_map = self.manager.data_loader.ohlc(
                    ticker, start_str, end_str, freq="d", now=True, adjust="qfq"
                )
                ohlcv = data_map.get(ticker)
            if ohlcv is None or ohlcv.empty:
                return self._fail(f"{ticker} 没有可用行情。", "NoMarketData")

            current_price = float(ohlcv["close"].iloc[-1])
            if "raw_close" in ohlcv.columns:
                current_raw = float(ohlcv["raw_close"].iloc[-1])
            else:
                current_raw = current_price

            mode_params = json.loads(position.mode_params or "{}")
            mode = getattr(position, "mode_instance", None) or self.manager._get_mode_instance(
                position.mode, position.mode_params
            )

            trades = self.manager.list_trade_record(position.pos_id)
            entry_price = None
            for item in trades:
                if item.type == BUY:
                    entry_price = float(item.price)
                    break
            first_entry = position.first_entry_date or position.created_at
            days_held = max(0, (now_cn() - first_entry).days) if first_entry else 0
            pyramid_level = int(mode_params.get("pyramid_level") or 0)

            info = {
                "current_price": current_price,
                "ohlcv": ohlcv,
                "days_held": days_held,
                "first_entry_price": entry_price or position.cost,
                "avg_cost": position.cost,
                "cost_price": position.cost,
                "quantity": position.quantity,
                # 真实层级：以前硬编码 0，导致加仓信号每次都从第 0 层重新触发
                "pyramid_level": pyramid_level,
                "has_added": pyramid_level > 0,
                "stop_price": position.stop_price,
            }
            add_signal = mode.check_add_signal(info)
            reduce_signal = mode.check_reduce_signal(info)

            return self._response(
                True,
                "Signals checked.",
                {
                    "ticker": ticker,
                    "current_price": current_raw,
                    "adjusted_price": current_price,
                    "add_signal": add_signal,
                    "reduce_signal": reduce_signal,
                    "stop_price": position.stop_price or mode.stop_price,
                    "pyramid_level": pyramid_level,
                    "days_held": days_held,
                },
            )
        except Exception as exc:
            return self._fail(f"计算信号异常: {exc}", type(exc).__name__)

    def get_buying_advice(
        self,
        portfolio_id: str,
        ticker: str,
        initial_risk_per_trade: float = None,
        mode: str = "ATR",
    ):
        ticker = normalize_code(ticker)
        summary = self.manager.get_portfolio_summary(portfolio_id)
        if "error" in summary:
            return self._fail(summary["error"], "NotFound")

        try:
            end_str = now_cn().strftime("%Y-%m-%d")
            start_str = (now_cn() - timedelta(days=120)).strftime("%Y-%m-%d")
            ohlcv = None
            if self.manager.data_loader:
                data_map = self.manager.data_loader.ohlc(
                    ticker, start_str, end_str, freq="d", now=True, adjust="qfq"
                )
                ohlcv = data_map.get(ticker)
            if ohlcv is None or ohlcv.empty:
                return self._fail(f"{ticker} 没有可用行情，无法给出建议。", "NoMarketData")

            from .modes import create_mode

            mode_instance = create_mode(
                mode, {"risk_per_trade": initial_risk_per_trade or 0.02}
            )
            advice = mode_instance.calculate_purchase_advice(
                {
                    "ohlcv": ohlcv,
                    "total_capital": summary.get("total_assets", 0),
                    "available_cash": summary.get("cash", 0),
                }
            )
            if not advice:
                return self._fail("无法计算建仓建议（ATR 或行情数据不足）。", "InsufficientData")
            return self._response(True, "Buying advice calculated.", advice)
        except Exception as exc:
            return self._fail(f"计算建仓建议异常: {exc}", type(exc).__name__)
