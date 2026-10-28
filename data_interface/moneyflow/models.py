"""分档数据的领域常量与源元数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 分档桶：小单 / 中单 / 大单 / 超大单
BUCKETS = ("sm", "md", "lg", "elg")

#: 源 ID（写入 ``moneyflow_daily.source``）
SOURCE_TUSHARE = "tushare.moneyflow"
SOURCE_AKSHARE_EM = "akshare.em"

#: 规范化后的列（与 ``scripts.schemas.moneyflow_schema.MONEYFLOW_COLUMNS`` 一致）
CANONICAL_COLUMNS = [
    "ts",
    "code",
    "market",
    "buy_sm_vol",
    "buy_sm_amount",
    "sell_sm_vol",
    "sell_sm_amount",
    "buy_md_vol",
    "buy_md_amount",
    "sell_md_vol",
    "sell_md_amount",
    "buy_lg_vol",
    "buy_lg_amount",
    "sell_lg_vol",
    "sell_lg_amount",
    "buy_elg_vol",
    "buy_elg_amount",
    "sell_elg_vol",
    "sell_elg_amount",
    "net_mf_vol",
    "net_mf_amount",
    "source",
    "source_version",
    "quality",
    "quality_note",
    "fetched_at",
]

#: 派生的横截面字段 -> SQL 表达式（不含成交额 join）
RANK_FIELDS: dict[str, str] = {
    "main_amount": (
        "(buy_lg_amount + buy_elg_amount) - (sell_lg_amount + sell_elg_amount)"
    ),
    "net_mf_amount": "net_mf_amount",
    "super_large_net": "buy_elg_amount - sell_elg_amount",
    "large_net": "buy_lg_amount - sell_lg_amount",
    "small_net": "buy_sm_amount - sell_sm_amount",
    "buy_amount": (
        "buy_sm_amount + buy_md_amount + buy_lg_amount + buy_elg_amount"
    ),
}


@dataclass(frozen=True)
class MoneyflowSourceMeta:
    source: str
    vendor: str
    dataset: str
    unit: str
    definition: str
    priority: int
    enabled: bool = True
    coverage_start: str | None = None
    coverage_end: str | None = None
    notes: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "vendor": self.vendor,
            "dataset": self.dataset,
            "unit": self.unit,
            "definition": self.definition,
            "priority": self.priority,
            "enabled": self.enabled,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "notes": self.notes,
        }


TUSHARE_META = MoneyflowSourceMeta(
    source=SOURCE_TUSHARE,
    vendor="tushare",
    dataset="moneyflow",
    unit="元/股（入库前由万元/手换算）",
    definition=(
        "按单笔成交金额分档（小/中/大/超大单），主动买/卖双向；"
        "金额原始单位万元、量原始单位手；net_mf 与四档净额口径不同"
    ),
    priority=10,
    coverage_start="2010-01-01",
    notes="单日全市场一次调用；实测 2026-10-27 覆盖 5577 只",
)
