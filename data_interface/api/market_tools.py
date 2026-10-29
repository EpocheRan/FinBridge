"""行情类 MCP 工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from data_interface.api.runtime import get_market_loader, get_trading_calendar
from data_interface.api.utils import dataframe_to_records, fail, from_exception, ok
from data_interface.common.timeutil import now_cn
from data_interface.market.adjust import (
    ADJUST_QFQ,
    VALID_ADJUST_MODES,
    frame_adjust_note,
)
from data_interface.market.constants import VALID_FREQUENCIES, normalize_frequency


def _valid_date(value: str | None) -> bool:
    """宽松日期校验：None 视为合法（表示「取最新」）。"""
    if value in (None, ""):
        return True
    try:
        pd.Timestamp(str(value))
        return True
    except Exception:
        return False


def register_market_tools(mcp) -> None:
    @mcp.tool()
    def get_ohlc_data(
        stock_codes: str | list[str],
        start_date: str,
        end_date: str,
        freq: str = "d",
        now: bool = False,
        adjust: str = ADJUST_QFQ,
    ) -> dict[str, Any]:
        """获取股票/ETF/LOF 的 OHLCV 行情。

        库里存的是不复权价 + 复权因子，这里按 ``adjust`` 现算，所以同一天在不同
        查询区间会返回一致的价格。

        Args:
            stock_codes: 单个代码或代码列表，如 "600519" 或 ["600519", "000001"]。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            freq: 1min / 5min / 15min / 30min / 60min / d / w / m。
            now: 是否拼接实时行情（仅交易时段、且查询区间覆盖今天时生效）。
            adjust: none=不复权，qfq=前复权（默认），hfq=后复权。
        """
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        if start_dt > end_dt:
            return fail("开始日期不能晚于结束日期", "InvalidDateRange", "请交换日期顺序。")
        try:
            freq = normalize_frequency(freq)
        except ValueError as exc:
            return fail(
                str(exc),
                "InvalidFrequency",
                f"可选值: {', '.join(VALID_FREQUENCIES)}",
            )

        try:
            data = get_market_loader().ohlc(
                stock_codes, start_date, end_date, freq, now=now, adjust=adjust
            )
        except ValueError as exc:
            return fail(
                str(exc),
                "InvalidAdjustMode",
                f"adjust 可选值: {', '.join(VALID_ADJUST_MODES)}",
            )
        except Exception as exc:
            return from_exception(exc)

        result: dict[str, Any] = {}
        warnings: list[str] = []
        for code, frame in data.items():
            note = frame_adjust_note(frame)
            if note.get("adjust_fallback") == "missing_adj_factor":
                warnings.append(f"{code} 缺少复权因子，已按不复权返回")
            result[code] = {
                "count": len(frame),
                "adjust": note.get("adjust"),
                "adjust_fallback": note.get("adjust_fallback"),
                "data": dataframe_to_records(frame, drop_columns=("raw_close", "adj_factor")),
            }

        if not any(item["count"] for item in result.values()):
            return fail(
                f"未找到 {stock_codes} 在 {start_date} ~ {end_date} 的行情",
                "NoMarketData",
                "请确认代码、日期范围，或先检查数据源/数据库是否可用。",
            )
        return ok(f"获取 {len(result)} 只标的的行情数据", result, warnings=warnings)

    @mcp.tool()
    def get_ticker_basic_info(stock_code: str) -> dict[str, Any]:
        """获取股票/ETF/LOF 的基础信息（含申万行业、上市板、股本、市值等）。"""
        try:
            frame = get_market_loader().ticker_basic_info(stock_code)
            if frame is None or frame.empty:
                return fail(f"未找到 {stock_code} 的基础信息", "NotFound", "请检查代码是否正确。")
            records = dataframe_to_records(frame)
            return ok(f"成功获取 {stock_code} 基础信息", {"data": records, "count": len(records)})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_realtime_quote(stock_code: str) -> dict[str, Any]:
        """获取单只标的的腾讯实时行情（不复权最新价）。"""
        try:
            frame = get_market_loader().fetcher.get_realtime_quotes(ticker=stock_code)
            records = dataframe_to_records(frame)
            if not records:
                return fail(f"未获取到 {stock_code} 的实时行情", "NoMarketData", "请稍后重试。")
            return ok(f"成功获取 {stock_code} 实时行情", {"data": records, "count": len(records)})
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_trading_days(start_date: str, end_date: str) -> dict[str, Any]:
        """获取指定区间的交易日列表。"""
        try:
            days = get_trading_calendar().get_trading_days(start_date, end_date)
            return ok(
                f"成功获取 {len(days)} 个交易日",
                {"data": days, "count": len(days), "start_date": start_date, "end_date": end_date},
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_intraday_metrics(
        stock_code: str,
        trade_date: str | None = None,
        freq: str = "1min",
        board: str = "MAIN",
        is_st: bool = False,
    ) -> dict[str, Any]:
        """获取单日分时结构指标（封板时间/炸板次数/尾盘竞价占比/均价线偏离/封板质量分）。

        Args:
            stock_code: 6 位证券代码。
            trade_date: 交易日 YYYY-MM-DD；不传取库内最新一根分时。
            freq: 1min 或 5min（5min 也能算，但封板时点精度下降）。
            board: MAIN/GEM/STAR/BJ，决定涨跌停幅度。
            is_st: 是否 ST（5% 涨跌停）。
        """
        from data_interface.market.intraday import metrics_to_dict, session_metrics

        if not _valid_date(trade_date):
            return fail("日期格式错误", "DateFormatError", "请使用 YYYY-MM-DD 格式。")
        try:
            db = get_market_loader().fetcher.db
            table = "ohlcv_1m" if freq == "1min" else "ohlcv_5m"
            target = trade_date
            if not target:
                row = db.query(
                    f'SELECT max(ts) FROM "{table}" WHERE code = %s', (stock_code,), fetch_all=False
                )
                if not row or row[0] is None:
                    return fail("分时表为空", "NoIntradayData", "请先运行 scripts/backfill_intraday.py。")
                target = str(pd.Timestamp(row[0]).date())
            frame = db.query_dataframe(
                f'SELECT ts, open, high, low, close, volume, amount FROM "{table}" '
                "WHERE code = %s AND ts BETWEEN %s AND %s ORDER BY ts",
                (stock_code, f"{target} 00:00:00", f"{target} 23:59:59"),
            )
            if frame is None or frame.empty:
                return fail(
                    f"{stock_code} 在 {target} 无分时数据",
                    "NoIntradayData",
                    "请先运行 scripts/backfill_intraday.py --codes <code>。",
                )
            prev = db.query_dataframe(
                'SELECT close FROM "ohlcv_1d" WHERE code = %s AND ts < %s ORDER BY ts DESC LIMIT 1',
                (stock_code, f"{target} 00:00:00"),
            )
            prev_close = float(prev["close"].iloc[0]) if prev is not None and not prev.empty else float(
                frame["close"].iloc[0]
            )
            metrics = session_metrics(frame, prev_close, board=board, is_st=is_st)
            if metrics is None:
                return fail("分时数据不足", "NoIntradayData", "至少需要两根分时。")
            return ok(
                f"{stock_code} {target} 分时结构指标",
                {"trade_date": target, "prev_close": prev_close, **metrics_to_dict(metrics)},
            )
        except Exception as exc:
            return from_exception(exc)

    @mcp.tool()
    def get_today_date() -> dict[str, Any]:
        """获取今天的日期和星期（北京时间）。"""
        current = now_cn()
        return ok(
            "成功获取今天日期",
            {"date": current.strftime("%Y-%m-%d"), "weekday": current.isoweekday()},
        )
