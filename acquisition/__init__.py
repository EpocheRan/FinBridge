"""整合同步（acquisition）。

替代旧定时任务模块的入口：DatasetSyncer 编排、进度条、Scope（全市场/单代码/板块 × 日期段）。
"""

from .orchestrator import DATASET_ORDER, Orchestrator, Scope, build_orchestrator

__all__ = ["DATASET_ORDER", "Orchestrator", "Scope", "build_orchestrator"]
