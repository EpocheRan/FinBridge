"""数据接口层（Data Interface）。

该包是行情、财务、交易日历等外部数据能力的统一入口，按职责拆分为：

- ``common``    通用连接、配置、限流、异常、交易日历
- ``market``    行情数据（OHLCV、实时行情、基础信息、概念板块）
- ``financial`` 财务数据（三大报表、估值、一致预期、板块归属）
- ``api``       对外暴露的 MCP 工具函数

上层（MCPserver、position）只依赖本包，不再直接依赖数据源 SDK。
"""

__all__ = ["common", "market", "financial", "api"]
