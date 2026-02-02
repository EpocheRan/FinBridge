# FinBridge MCP - 量化交易数据与持仓管理系统

## 快速安装与配置

### 1. 启动服务 (推荐 Docker)
在项目根目录下执行：
```bash
docker-compose up -d
```
> **注意**：在运行 Docker 时，请确保将 `quantProject` 所在的**项目目录共享（Share/Mount）给容器**，以便 Docker 环境能够正确访问 Python 代码、加载本地模块并读取配置文件 (`config.json`)。

### 2. 本地运行 (非 Docker)
1. **安装 QuestDB**：访问 [QuestDB 官网](https://questdb.io/) 下载并安装，确保服务在 8812 端口运行。
2. **修改配置**：编辑 `config.json`，将 `"host": "questdb"` 调整为 `"host": "localhost"`。
3. **切换服务器运行方式**（可选）：
   - 默认使用 **SSE** 方式（适用于远程/Web 访问，端口 8080）。
   - 若需使用 **stdio** 方式（适用于 MCP Inspector 或直接命令行对接），请修改 `MCPserver.py` 最后几行：
     ```python
     # 注释掉 SSE 方式
     # mcp.run(transport="sse", host="0.0.0.0", port=8080)
     # 取消注释 stdio 方式
     mcp.run()
     ```
4. **安装依赖并启动**：
    ```bash
    uv pip install -e .  # 或 pip install -e .
    python MCPserver.py
    ```

### 3. SSE 客户端配置示例
若使用 Claude Desktop 或其他支持 MCP SSE 的客户端，配置如下：
- **类型 (Type)**: `sse`
- **URL**: `http://localhost:8080/sse`

### 4. 数据持久化建议
为了防止容器删除后数据丢失，建议为 QuestDB 配置持久化存储：
- **Docker 方式**：取消 `docker-compose.yml` 中 `questdb` 服务下 `volumes` 的注释，将数据映射到本地 `./questdb_data` 目录。
- **本地方式**：QuestDB 默认会将数据存储在其安装目录的 `db` 文件夹下，请定期备份该目录。

---


## 1. 项目概述
QuantProject 是基于 **Model Context Protocol (MCP)** 的量化交易管理系统，旨在为 AI Agent 提供标准化的金融数据接口和专业的持仓风控功能。

- **数据层**：聚合 Akshare、Baostock，支持增量更新至 QuestDB。
- **协议层**：基于 FastMCP 暴露标准化工具函数。
- **业务层**：ATR 波动率仓位管理、风险控制、资金管理。

---

## 2. 核心工具函数 (MCP Tools)

| 类别 | 工具函数 | 功能描述 |
| :--- | :--- | :--- |
| **行情数据** | `get_ohlc_data` | 获取股票/指数 OHLCV 数据 (多频率) |
| **财务/基础** | `get_rolling_finance`, `get_ticker_basic_info` | 财务指标、证券基本信息 |
| **持仓管理** | `pm_create_portfolio`, `pm_execute_trade` | 创建组合、记录交易、平仓 |
| **策略/风控** | `pm_check_signals`, `pm_get_buying_advice` | 检查加减仓信号、基于 ATR 的买入建议 |

---

## 3. 使用示例

### 获取行情
```python
# 通过 MCP 客户端调用
result = await get_ohlc_data(stock_codes=["600519"], start_date="2024-01-01", freq="d")
```

### 交易管理
```python
# 1. 创建组合
portfolio = await pm_create_portfolio(name="TechGrowth", initial_cash=1000000)

# 2. 记录交易
await pm_execute_trade(portfolio_id=id, ticker="600519", action="BUY", quantity=100, price=1800.0)
```

---

## 4. 项目结构
```text
quantProject/
├── MCPserver.py        # MCP 服务器入口
├── config.json         # 数据库与系统配置
├── utils/              # 数据加载、抓取与连接管理
├── position/           # 持仓逻辑、ATR 仓位管理与 ORM 模型
├── indicator/          # 技术指标计算 (Backtrader 封装)
└── scripts/            # 数据库初始化脚本
```

---

## 5. 注意事项
1. **数据库**：默认使用 QuestDB (端口 8812)，存储行情与持仓数据。
2. **频率支持**：支持 1m, 5m, 15m, 30m, 60m, d, w, m。
3. **数据更新**：系统会自动识别缺失日期并执行增量更新。
4. **日志**：运行日志详见 `logs/mcp_server.log`。
