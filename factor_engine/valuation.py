"""估值因子（valuation）：PE/PB/PS/PCF/PFCF/EV/股息率等。

数据来源：``fin_valuation_daily``（财务服务按 PIT 口径写入的每日估值）。
这些指标在旧设计里是 screener 的筛选列；现在单独归入 ``Category.VALUATION``，
与价格/技术/成长/质量因子分离，仍写入 ``factor_value_daily`` 长表。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from factors.spec import FactorSpec, Materialization, SourceSpec
from factors.taxonomy import Category

logger = logging.getLogger("FactorEngine.Valuation")


def _spec(
    column: str,
    name: str,
    subcategory: str,
    *,
    unit: str = "ratio",
    direction: str = "negative",
) -> FactorSpec:
    return FactorSpec(
        factor_id=f"valuation.{column}",
        name=name,
        category=Category.VALUATION,
        subcategory=subcategory,
        source=SourceSpec("fin_valuation_daily", "questdb", (column,), "trade_date"),
        window=0,
        freq="1d",
        value_kind="daily",
        update_mode="shift",
        unit=unit,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        price_basis="none",
        version="v1",
        source_version_policy="val",
        warmup=0,
        formula=f"fin_valuation_daily.{column}",
        aliases=(column,),
        materialization=Materialization.STORED,
    )


VALUATION_SPECS: tuple[FactorSpec, ...] = (
    _spec("pe_ttm", "市盈率TTM", "earnings"),
    _spec("pb_mrq", "市净率MRQ", "asset"),
    _spec("ps_ttm", "市销率TTM", "income"),
    _spec("pcf_ttm", "市现率TTM", "cash_dividend"),
    _spec("pfcf", "自由现金流市现率", "cash_dividend"),
    _spec("ev_ebitda", "EV/EBITDA", "income"),
    _spec("ev_ebit", "EV/EBIT", "income"),
    _spec("ev_sales", "EV/营收", "income"),
    _spec("ev_gross_profit", "EV/毛利", "income"),
    _spec("ev_fcf", "EV/自由现金流", "income"),
    _spec("p_nwc", "市值/净营运资本", "income"),
    _spec("earnings_yield", "盈利收益率", "earnings", direction="positive"),
    _spec("dividend_yield", "股息率", "cash_dividend", unit="pct", direction="positive"),
)

#: factor_id -> fin_valuation_daily 列
VALUATION_COLUMNS: dict[str, str] = {
    spec.factor_id: spec.source.fields[0] for spec in VALUATION_SPECS
}

#: 多源并存时的优先级（数字越大越优先）
_SOURCE_PRIORITY = {"tushare_daily_basic": 2, "akshare_indicator": 1}


class ValuationComputer:
    """从 ``fin_valuation_daily`` 读取估值指标并转成因子长表。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db: QuestDBClient | None = None,
    ):
        self.config = config or get_config()
        self.db = db or QuestDBClient(self.config, connect=False)

    def load_metrics(
        self,
        codes: Sequence[str] | None,
        start,
        end,
    ) -> pd.DataFrame:
        columns = sorted(set(VALUATION_COLUMNS.values()))
        select = ", ".join(["ts", "code", "source", *columns])
        sql = (
            "SELECT "
            + select
            + ' FROM "fin_valuation_daily" '
            "WHERE ts >= CAST(%s AS TIMESTAMP) AND ts < CAST(%s AS TIMESTAMP)"
        )
        start_text = str(pd.Timestamp(start).date())
        end_text = str((pd.Timestamp(end).normalize() + pd.Timedelta(days=1)).date())
        try:
            frame = self.db.query_dataframe(sql, (start_text, end_text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 fin_valuation_daily 失败: %s", exc)
            return pd.DataFrame(columns=["ts", "code", *columns])
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts", "code", *columns])
        frame = frame.copy()
        if "ts" not in frame.columns:
            frame = frame.reset_index()  # query_dataframe 会把 ts 设为索引
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce").dt.normalize()
        frame["code"] = frame["code"].astype(str)
        if codes:
            wanted = {str(code) for code in codes}
            frame = frame[frame["code"].isin(wanted)]
        # 多源并存：按源优先级去重，每个 (ts, code) 只保留最高优先级的一行
        if "source" in frame.columns:
            frame["_pri"] = frame["source"].map(_SOURCE_PRIORITY).fillna(0)
            frame = frame.sort_values(["ts", "code", "_pri"]).drop_duplicates(
                ["ts", "code"], keep="last"
            )
            frame = frame.drop(columns=["_pri"])
        for column in columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["ts"]).reset_index(drop=True)

    def compute(self, metrics: pd.DataFrame) -> pd.DataFrame:
        """metrics(ts, code, <列>) -> 长表(ts, code, factor_id, value)。"""
        if metrics is None or metrics.empty:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        frames: list[pd.DataFrame] = []
        for factor_id, column in VALUATION_COLUMNS.items():
            if column not in metrics.columns:
                continue
            part = metrics[["ts", "code", column]].copy()
            part["value"] = pd.to_numeric(part[column], errors="coerce")
            part = part.dropna(subset=["value"])
            if part.empty:
                continue
            part["factor_id"] = factor_id
            frames.append(part[["ts", "code", "factor_id", "value"]])
        if not frames:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        return pd.concat(frames, ignore_index=True)[
            ["ts", "code", "factor_id", "value"]
        ]
