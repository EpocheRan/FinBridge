"""因子研究：IC、分层收益、换手与衰减。"""

from .metrics import (
    decay,
    forward_returns,
    ic,
    ic_summary,
    quantile_returns,
    turnover,
)
from .service import ResearchReport, parse_factor_name, run_research

__all__ = [
    "ResearchReport",
    "decay",
    "forward_returns",
    "ic",
    "ic_summary",
    "parse_factor_name",
    "quantile_returns",
    "run_research",
    "turnover",
]
