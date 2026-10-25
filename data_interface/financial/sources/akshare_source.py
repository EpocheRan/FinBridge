"""AkShare 财务/板块补充数据源。

定位：
- Tushare 为主源，AkShare 负责基础信息、板块归属、以及一致预期的免费兜底。
- AkShare 是网页接口封装，字段可能随上游变化；所有调用都做异常隔离，
  由上层决定是否降级。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from data_interface.common.config import get_config
from data_interface.common.exceptions import SourceUnavailableError

from ..normalizer import normalize_code, parse_date, safe_float

logger = logging.getLogger("DataInterface.AkShareFinancial")


class AkShareFinancialSource:
    name = "akshare"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or get_config()

    @staticmethod
    def _ak():
        try:
            import akshare as ak
        except ImportError as exc:
            raise SourceUnavailableError("未安装 akshare，请执行 uv sync", source="akshare") from exc
        return ak

    # ------------------------------------------------------------------ 基础信息
    def fetch_profile(self, code: str) -> dict[str, Any] | None:
        code = normalize_code(code)
        ak = self._ak()
        result: dict[str, Any] = {"code": code, "source": "akshare"}

        try:
            info = ak.stock_individual_info_em(symbol=code)
            mapping = dict(zip(info["item"], info["value"]))
            result.update(
                {
                    "name": mapping.get("股票简称"),
                    "industry": mapping.get("行业"),
                    "list_date": parse_date(mapping.get("上市时间")),
                    "total_share": safe_float(mapping.get("总股本")),
                    "float_share": safe_float(mapping.get("流通股")),
                    "total_mv": safe_float(mapping.get("总市值")),
                    "float_mv": safe_float(mapping.get("流通市值")),
                }
            )
        except Exception as exc:
            logger.warning("AkShare stock_individual_info_em 失败 %s: %s", code, exc)

        try:
            profile = ak.stock_profile_cninfo(symbol=code)
            if not profile.empty:
                result["fullname"] = profile["公司名称"].iloc[0] if "公司名称" in profile.columns else None
                result["main_business"] = profile["主营业务"].iloc[0] if "主营业务" in profile.columns else None
        except Exception as exc:
            logger.debug("AkShare stock_profile_cninfo 失败 %s: %s", code, exc)

        return result if any(result.get(k) for k in ("name", "industry", "total_share")) else None

    # ------------------------------------------------------------------ 板块
    def fetch_board_catalog(self, board_type: str = "industry") -> list[dict[str, Any]]:
        """获取东财行业/概念板块目录。"""
        ak = self._ak()
        if board_type == "concept":
            df = ak.stock_board_concept_name_em()
            id_col, name_col = "板块代码", "板块名称"
        else:
            df = ak.stock_board_industry_name_em()
            id_col, name_col = "板块代码", "板块名称"
        rows = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "board_id": str(row.get(id_col)),
                    "board_type": f"em_{board_type}",
                    "board_name": str(row.get(name_col)),
                    "parent_id": None,
                    "source": "akshare",
                }
            )
        return rows

    def fetch_board_members(self, board_name: str, board_type: str = "industry") -> list[dict[str, Any]]:
        """获取某个东财行业/概念板块的成分股。"""
        ak = self._ak()
        if board_type == "concept":
            df = ak.stock_board_concept_cons_em(symbol=board_name)
        else:
            df = ak.stock_board_industry_cons_em(symbol=board_name)
        rows = []
        for _, row in df.iterrows():
            code = normalize_code(row.get("代码") or row.get("股票代码") or "")
            if not code:
                continue
            rows.append(
                {
                    "code": code,
                    "board_name": board_name,
                    "board_type": f"em_{board_type}",
                    "board_id": board_name,
                }
            )
        return rows

    # ------------------------------------------------------------------ 估值
    def fetch_valuation(
        self, code: str, *, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """乐咕乐股估值历史，作为 Tushare 的备用源。"""
        code = normalize_code(code)
        ak = self._ak()
        try:
            df = ak.stock_a_indicator_lg(symbol=code)
        except Exception as exc:
            raise SourceUnavailableError(
                f"AkShare 估值接口失败 {code}: {exc}", source="akshare"
            ) from exc
        if df is None or df.empty:
            return []

        df["trade_date"] = df["trade_date"].astype(str)
        df = df[(df["trade_date"] >= start_date.replace("-", "")) & (df["trade_date"] <= end_date.replace("-", ""))]
        rows = []
        for _, item in df.iterrows():
            ts = parse_date(item["trade_date"])
            if ts is None:
                continue
            total_mv = safe_float(item.get("total_mv"))
            # stock_a_indicator_lg 的 total_mv 单位为亿元
            market_cap = total_mv * 100_000_000 if total_mv is not None else None
            dividend_yield = safe_float(item.get("dv_ttm"))
            if dividend_yield is not None:
                dividend_yield /= 100.0
            rows.append(
                {
                    "ts": ts,
                    "code": code,
                    "close": None,
                    "total_share": None,
                    "market_cap": market_cap,
                    "float_market_cap": None,
                    "enterprise_value": None,
                    "pe_ttm": safe_float(item.get("pe_ttm")),
                    "pb_mrq": safe_float(item.get("pb")),
                    "ps_ttm": safe_float(item.get("ps_ttm")),
                    "pcf_ttm": None,
                    "pfcf": None,
                    "ev_ebitda": None,
                    "ev_ebit": None,
                    "ev_sales": None,
                    "ev_gross_profit": None,
                    "ev_fcf": None,
                    "p_nwc": None,
                    "earnings_yield": None,
                    "dividend_yield": dividend_yield,
                    "market_cap_change_pct": None,
                    "source": "akshare_indicator",
                    "formula_version": "v1",
                    "ingested_at": datetime.utcnow(),
                }
            )
        return rows

    # ------------------------------------------------------------------ 一致预期
    def fetch_estimates(self, code: str) -> list[dict[str, Any]]:
        """同花顺盈利预测（EPS / 净利润），作为 Tushare report_rc 的兜底。"""
        code = normalize_code(code)
        ak = self._ak()
        rows: list[dict[str, Any]] = []
        as_of = datetime.utcnow()

        for indicator, metric, unit, scale in (
            ("预测年报每股收益", "eps", "CNY/SHARE", 1.0),
            ("预测年报净利润", "net_income", "CNY", 100_000_000.0),
        ):
            try:
                df = ak.stock_profit_forecast_ths(symbol=code, indicator=indicator)
            except Exception as exc:
                logger.warning("AkShare 盈利预测失败 %s/%s: %s", code, indicator, exc)
                continue
            if df is None or df.empty:
                continue
            for _, item in df.iterrows():
                year = item.get("年度")
                try:
                    fiscal_year = int(year)
                except (TypeError, ValueError):
                    continue
                rows.append(
                    {
                        "as_of": as_of,
                        "code": code,
                        "metric": metric,
                        "fiscal_year": fiscal_year,
                        "fiscal_period": "FY",
                        "horizon": "FY",
                        "value_mean": safe_float(item.get("均值")),
                        "value_median": None,
                        "value_high": safe_float(item.get("最大值")),
                        "value_low": safe_float(item.get("最小值")),
                        "analyst_count": int(item.get("预测机构数") or 0),
                        "unit": unit,
                        "source": "akshare_profit_forecast",
                        "source_updated_at": as_of,
                        "ingested_at": as_of,
                        # 净利润原始单位为亿元
                        "_scale": scale,
                    }
                )

        for row in rows:
            scale = row.pop("_scale", 1.0)
            if row["value_mean"] is not None:
                row["value_mean"] *= scale
            if row["value_high"] is not None:
                row["value_high"] *= scale
            if row["value_low"] is not None:
                row["value_low"] *= scale
        return rows


