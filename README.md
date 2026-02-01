# QuantProject - 量化交易数据与持仓管理系统

## 1. 项目概述

QuantProject 是一个基于 **Model Context Protocol (MCP)** 标准开发的量化交易数据与持仓管理系统。该项目主要用于连接底层金融数据源与上层的 AI Agent 或自动化交易策略，提供标准化的数据查询、技术指标计算以及专业的持仓风控管理功能。

### 核心定位

- **数据层**：聚合多个数据源（Akshare、Baostock），提供统一的股票、ETF、概念板块等金融数据访问接口
- **协议层**：基于 MCP 协议暴露标准化工具函数，支持大语言模型直接调用进行量化分析
- **业务层**：提供完整的持仓管理、风险控制、资金管理解决方案

### 技术特点

- 采用 FastMCP 框架构建 MCP 服务端
- 支持增量数据更新，避免重复获取
- 基于 ATR 的专业波动率仓位管理
- 支持多频率数据（日线、分钟线、周月线）

---

## 2. 功能模块

### 2.1 MCP 服务器核心 (`MCPserver.py`)

作为系统的统一入口，基于 `fastmcp` 框架构建，通过标准协议暴露工具函数。

**主要功能**：

| 工具函数 | 功能描述 |
|---------|---------|
| `get_ohlc_data` | 获取股票 OHLCV 行情数据，支持多频率 |
| `get_rolling_finance` | 获取滚动财务指标数据 |
| `get_ticker_basic_info` | 获取股票/基金基本信息 |
| `get_trading_days` | 获取指定日期范围内的交易日列表 |
| `get_data_status` | 获取数据源状态信息 |
| `pm_create_portfolio` | 创建投资组合 |
| `pm_list_portfolios` | 列出所有投资组合 |
| `pm_open_position` | 开立跟踪仓位 |
| `pm_execute_trade` | 记录买入/卖出交易 |
| `pm_close_position` | 平仓操作 |
| `pm_check_signals` | 检查加仓/减仓信号 |

**响应格式**：

所有工具函数返回标准化的响应格式：

```json
{
    "success": true,
    "message": "成功获取 2 只股票的行情数据",
    "result": {
        "600519": {"data": [...], "count": 100},
        "000001": {"data": [...], "count": 100}
    }
}
```

### 2.2 数据管理模块 (`utils/`)

#### 2.2.1 DataLoader (`utils/DataLoader.py`)

数据加载器的核心类，负责集成各个数据源的数据，包含数据拼装和更新逻辑。

**支持的数据类型**：

| 方法 | 数据类型 | 更新策略 |
|-----|---------|---------|
| `ohlc()` | OHLCV 行情数据 | 增量更新 |
| `rolling_finance()` | 滚动财务指标 | 增量更新 |
| `ticker_basic_info()` | 证券基本信息 | 最新交易日更新 |
| `financial_indicators()` | 季度财务指标 | 季度更新 |
| `stock_industry()` | 行业分类数据 | 最新交易日更新 |
| `concept_boards()` | 概念板块列表 | 最新交易日更新 |
| `concept_boards_ohlc()` | 概念指数行情 | 增量更新 |
| `concept_constituents()` | 概念板块成分股 | 最新交易日更新 |

**更新策略**：

- **增量更新 (`_incremental_update`)**：优先从本地数据库获取数据，识别缺失日期范围，从 API 获取增量数据并回写数据库
- **最新交易日更新 (`_lastest_tradingday_update`)**：检查本地数据是否已包含最新交易日，否则从 API 获取
- **季度更新 (`_quarter_update`)**：财务数据按季度获取，自动映射每日对应的财报数据

#### 2.2.2 DataFetcher (`utils/DataFetcher.py`)

数据获取器，支持从不同数据源获取数据。

**支持的数据源**：

| 数据源 | 类型 | 主要用途 |
|-------|------|---------|
| `questdb` | 本地数据库 | 读取已存储的历史数据 |
| `akshare` | 第三方 API | 实时行情、基础信息 |
| `baostock` | 第三方 API | 历史行情、财务数据 |

#### 2.2.3 DataConnect (`utils/DataConnect.py`)

数据库连接管理，封装 QuestDB 和 Baostock 的连接操作。

**主要类**：

- `questDB`：PostgreSQL 协议连接 QuestDB，支持原生 SQL 执行
- `BS`：Baostock API 登录管理

#### 2.2.4 TradingDateManage (`utils/TradingDateManage.py`)

交易日历管理，处理复杂的 A 股交易日逻辑。

### 2.3 持仓与风控模块 (`position/`)

#### 2.3.1 数据模型 (`position/schema.py`)

基于 SQLAlchemy 定义的数据模型：

| 模型 | 描述 | 主要字段 |
|-----|------|---------|
| `PortfolioModel` | 投资组合 | id, name, total_assets, cash, market_value |
| `PositionModel` | 持仓记录 | pos_id, ticker, quantity, cost, stop_price |
| `TradeRecordModel` | 交易记录 | trade_id, type, quantity, price, trade_date |

#### 2.3.2 PositionManager (`position/manager.py`)

持仓管理器，提供持仓的完整生命周期管理。

**核心功能**：

- **仓位计算**：基于 ATR（平均真实波幅）的波动率仓位管理
- **止损设置**：自动计算并更新止损价位
- **信号检测**：检测加仓/减仓信号（金字塔加仓策略）
- **风险监控**：僵尸仓位检测

**支持的仓位管理模式**：

| 模式 | 描述 |
|-----|------|
| `FIXED_RISK` | 固定风险比例 |
| `VOLATILITY_ADJUSTED` | 波动率调整仓位 |
| `ATR_BASED` | 基于 ATR 的仓位计算 |

#### 2.3.3 PositionAgentInterface (`position/llm_tool.py`)

封装持仓管理功能为 Agent 友好的接口，与 MCP 服务端对接。

### 2.4 指标计算引擎 (`indicator/`)

基于 `backtrader` 封装的轻量级技术指标计算引擎。

### 2.5 初始化脚本 (`scripts/`)

| 脚本 | 功能描述 |
|-----|---------|
| `initialize.py` | 初始化数据库表结构 |
| `init_position_db.py` | 初始化持仓数据库 |
| `init_market_data_db.py` | 初始化行情数据库 |

---

## 3. 模块交互

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        AI Agent / Client                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     MCP Server (MCPserver.py)                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Tool Functions (get_ohlc_data, pm_execute_trade...)    │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Position Interface                           │
│                    (position/llm_tool.py)                       │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
┌─────────────────────┐             ┌─────────────────────────────┐
│  PositionManager    │             │      DataLoader             │
│  (position/)        │             │      (utils/)               │
│  - Portfolio        │             │  - ohlc()                   │
│  - Position         │             │  - rolling_finance()        │
│  - TradeRecord      │             │  - ticker_basic_info()      │
└─────────────────────┘             └─────────────────────────────┘
                                              │
          ┌───────────────────┬───────────────┴───────────────┐
          ▼                   ▼                               ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────────────┐
│   QuestDB       │ │   Akshare API   │ │     Baostock API        │
│   (PostgreSQL)  │ │                 │ │                         │
└─────────────────┘ └─────────────────┘ └─────────────────────────┘ 
```

### 3.2 数据流程

#### 3.2.1 行情数据获取流程

```
1. Agent 调用 get_ohlc_data(stock_codes, start_date, end_date, freq)
              │
              ▼
2. MCP Server 调用 DataLoader.ohlc()
              │
              ▼
3. DataLoader._incremental_update()
              │
              ├─▶ 3.1 尝试从 QuestDB 获取本地数据
              │
              ├─▶ 3.2 检查数据完整性，识别缺失日期范围
              │
              ├─▶ 3.3 从 API (Baostock/Akshare) 获取缺失数据
              │
              └─▶ 3.4 将增量数据写入 QuestDB
              │
              ▼
4. 合并数据并返回给 Agent
```

#### 3.2.2 持仓管理流程

```
1. Agent 调用 pm_create_portfolio(name, initial_cash)
              │
              ▼
2. PositionAgentInterface.create_portfolio()
              │
              ▼
3. PositionManager.create_portfolio()
              │
              ▼
4. 写入 PostgreSQL 数据库 (portfolios 表)
              │
              ▼
5. 返回 portfolio_id 给 Agent
```

### 3.3 模块依赖关系

```
MCPserver.py
    ├── DataLoader (utils/DataLoader.py)
    │   ├── DataFetcher (utils/DataFetcher.py)
    │   │   ├── questDB (utils/DataConnect.py)
    │   │   ├── BS/Baostock (utils/DataConnect.py)
    │   │   └── akshare
    │   └── TradingDateManage (utils/TradingDateManage.py)
    │
    ├── PositionAgentInterface (position/llm_tool.py)
    │   └── PositionManager (position/manager.py)
    │       └── PortfolioModel/PositionModel/TradeRecordModel
    │           └── SQLAlchemy + psycopg2 → QuestDB
    │
    └── check_and_initialize_tables (scripts/initialize.py)
```

---

## 4. 项目依赖

### 4.1 核心依赖

| 依赖库 | 版本要求 | 用途 |
|-------|---------|------|
| `fastmcp` | >= 3.0.0b1 | 实现 Model Context Protocol 协议，构建 Agent 服务端 |
| `akshare` | >= 1.18.20 | 获取 A 股市场的实时与历史金融数据 |
| `baostock` | >= 0.8.9 | 备用数据源，提供历史行情和财务数据 |
| `backtrader` | >= 1.9.78.123 | 专业的量化回测框架，用于技术指标的核心计算 |
| `pandas` | - | 高性能的数据清洗与处理 |
| `sqlalchemy` | >= 2.0.46 | 数据库 ORM 框架 |
| `psycopg2` | >= 2.9.11 | PostgreSQL 数据库驱动（QuestDB） |

### 4.2 间接依赖

| 依赖 | 用途 |
|-----|------|
| `requests` | HTTP 请求，用于 API 调用 |
| `numpy` | 数值计算，psycopg2 类型适配 |
| `tqdm` | 进度条显示 |

### 4.3 数据库依赖

| 数据库 | 协议 | 用途 |
|-------|------|------|
| **QuestDB** | PostgreSQL (端口 8812) | 存储行情数据、持仓数据、财务数据 |

### 4.4 安装依赖

```bash
# 使用 uv 安装（推荐）
uv pip install -e .

# 或使用 pip
pip install -e .
```

### 4.5 环境配置

项目根目录下的 `config.json` 文件配置数据库连接：

```json
{
    "questdb": {
        "host": "localhost",
        "port": 8812,
        "user": "admin",
        "password": "your_password",
        "database": "stocks"
    }
}
```

---

## 5. 快速开始

### 5.1 环境准备

1. 安装 Python 3.10+
2. 安装 QuestDB 并确保服务运行在 localhost:8812
3. 克隆项目并安装依赖：

```bash
git clone <repository_url>
cd quantProject
uv pip install -e .
```

### 5.2 启动 MCP 服务器

```bash
python MCPserver.py
```

服务器将以 stdio 方式运行，等待 Agent 客户端连接。

### 5.3 使用示例

#### 获取股票行情数据

```python
# 通过 MCP 工具调用
result = await get_ohlc_data(
    stock_codes=["600519", "000001"],
    start_date="2024-01-01",
    end_date="2024-12-31",
    freq="d"
)
```

#### 创建投资组合并建仓

```python
# 1. 创建组合
portfolio = await pm_create_portfolio(name="tech_growth", initial_cash=100000)

# 2. 获取买入建议
advice = await pm_get_buying_advice(
    portfolio_id=portfolio_id,
    ticker="600519",
    initial_risk_per_trade=0.02
)

# 3. 执行买入
await pm_execute_trade(
    portfolio_id=portfolio_id,
    ticker="600519",
    action="BUY",
    quantity=100,
    price=1800.0
)
```

---

## 6. 项目结构

```
quantProject/
├── MCPserver.py              # MCP 服务器入口
├── config.json               # 数据库配置
├── pyproject.toml            # 项目配置
├── AgentPrompt.md            # Agent 提示词
│
├── utils/                    # 工具模块
│   ├── __init__.py
│   ├── DataLoader.py         # 数据加载器
│   ├── DataFetcher.py        # 数据获取器
│   ├── DataConnect.py        # 数据库连接
│   └── TradingDateManage.py  # 交易日历
│
├── position/                 # 持仓管理模块
│   ├── __init__.py
│   ├── schema.py             # 数据模型
│   ├── manager.py            # 持仓管理器
│   └── llm_tool.py           # Agent 接口
│
├── indicator/                # 技术指标模块
│   └── ...
│
├── scripts/                  # 初始化脚本
│   ├── initialize.py
│   ├── init_position_db.py
│   └── init_market_data_db.py
│
├── tests/                    # 测试文件
│   ├── test_position_questdb.py
│   ├── test_position_manager.py
│   ├── test_ticker_basic.py
│   └── test_llm_interface.py
│
└── logs/                     # 日志目录
    ├── mcp_server.log
    └── stdout.log
```

---

## 7. 日志系统

项目内置完善的日志记录系统：

- **日志位置**：`logs/mcp_server.log`
- **日志轮转**：每天轮转，保留 7 天，单文件最大 10MB
- **日志级别**：INFO 级别记录主要操作

---

## 8. 注意事项

1. **数据源优先级**：系统优先使用本地 QuestDB 数据，缺失时才从 API 获取
2. **频率支持**：OHLC 数据支持 1min、5min、15min、30min、60min、d、w、m 频率
3. **交易日判断**：实时行情获取会自动判断当前是否为交易时间
4. **风险控制**：建议在实盘使用前充分测试仓位管理策略
