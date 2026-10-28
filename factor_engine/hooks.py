"""入库事件钩子：raw 数据集写完 -> 触发因子计算。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from data_interface.common.events import DataWritten

logger = logging.getLogger("FactorEngine.Hooks")

#: 数据集 -> 需要刷新的因子
DATASET_FACTORS: dict[str, tuple[str, ...]] = {
    "ohlcv_1d": (
        "momentum.ret",
        "reversal.short",
        "technical.atr",
        "risk.vol",
        "liquidity.turnover",
    ),
}


class FactorHooks:
    def __init__(self, service: Any):
        self.service = service

    def factor_ids_for(self, dataset: str) -> tuple[str, ...]:
        return DATASET_FACTORS.get(str(dataset), ())

    def on_data_written(self, event: DataWritten) -> dict[str, Any] | None:
        factor_ids = self.factor_ids_for(event.dataset)
        if not factor_ids:
            return None
        if not event.dates:
            return {"skipped": True, "reason": "no dates", "dataset": event.dataset}
        start = min(event.dates)
        end = max(event.dates)
        try:
            result = self.service.build(
                list(factor_ids),
                start,
                end,
                event.codes or None,
            )
            return {"dataset": event.dataset, **result}
        except Exception as exc:  # 失败落状态，交给重试；不阻断主同步
            logger.warning("因子钩子失败 %s: %s", event.dataset, exc)
            job_id = f"hook:{uuid.uuid4().hex[:12]}"
            try:
                self.service.store.upsert_build_state(
                    {
                        "job_id": job_id,
                        "dataset": event.dataset,
                        "factor_ids": ",".join(factor_ids),
                        "window_start": start,
                        "window_end": end,
                        "status": "FAILED",
                        "attempts": 1,
                        "last_error": f"{type(exc).__name__}: {exc}",
                        "payload": json.dumps({"codes": list(event.codes)}),
                    }
                )
            except Exception:
                logger.exception("写入因子失败状态也失败")
            return {
                "dataset": event.dataset,
                "job_id": job_id,
                "error": str(exc),
                "retryable": True,
            }
