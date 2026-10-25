"""领域时钟：统一北京时间（naive）。

分层规则要求 domain 不依赖 ``data_interface``，因此这里保留一份极小的
纯 stdlib 实现；语义与 ``data_interface.common.timeutil.now_cn`` 完全一致：

- A 股业务时间一律按 UTC+8（中国自 1991 年起无夏令时）；
- 写库/比较用 **naive** 时间，与 QuestDB ``TIMESTAMP``、``trade_date`` 对齐；
- 不依赖宿主机/容器的 TZ 设置。

以前 screener 各层混用 ``datetime.now()``：容器 TZ=UTC 时 build_state 的
租约、retry_at、computed_at 都会比业务时间差 8 小时。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_local() -> datetime:
    """当前北京时间（naive，用于写库与比较）。"""
    return datetime.now(CN_TZ).replace(tzinfo=None)
