# 11 · MCPserver（对外工具服务器）

## 1. 架构与依赖

### 1.1 职责边界

`MCPserver.py` 是**唯一的对外入口**：把各模块的能力注册成 MCP 工具，供 LLM/客户端调用。
它本身不含业务逻辑，只做三件事：

1. **注册工具**：调用各模块的 `register_*_tools(mcp)`；
2. **管理传输**：`stdio` / `http`（默认 http）；
3. **生命周期**：日志、信号处理、单例复用。

依赖方向是"服务器 → 各模块"，任何模块都不反向依赖它。

### 1.2 上下游

| 方向 | 模块 | 工具来源 |
|---|---|---|
| 下游 | `data_interface.api` | `market_tools` / `financial_tools` / `moneyflow_tools` / `concept_tools` / `disclosure_tools` |
| 下游 | `factor_engine.api` | 因子查询/物化 |
| 下游 | `acquisition.api` / `acquisition.legacy_tools` | 同步、定时任务 |
| 下游 | `screener.interface.api` | 选股 |
| 下游 | `position.llm_tool`（经 `MCPserver.py` 的 `pm_*`） | 持仓管理 |
| 存储 | QuestDB | 所有工具最终读写它 |

### 1.3 关键文件

| 文件 | 作用 |
|---|---|
| `MCPserver.py` | 服务器主体：`FastMCP` 实例、工具注册、`_resolve_transport`、信号处理 |
| `data_interface/api/*_tools.py` | 各域工具（每个文件 1 个 `register_*_tools`） |
| `data_interface/api/runtime.py` | 单例工厂：`get_market_loader` / `get_financial_service` / … |
| `data_interface/api/utils.py` | `ok` / `fail` / `json_safe` / `dataframe_to_records` |
| `factor_engine/api.py` / `acquisition/api.py` / `screener/interface/api.py` | 对应域工具 |

### 1.4 工具结构

```
MCPserver
├─ market      get_ohlc / get_intraday_metrics / get_stock_basic ...
├─ financial   get_financial_statements / get_valuation / get_indicators ...
├─ moneyflow   get_moneyflow / rank_moneyflow ...
├─ concept     get_stock_boards / get_board_members ...
├─ disclosure  get_disclosure_calendar / get_announcements ...
├─ factor      list_factors / get_factor_values / build_factors ...
├─ acquisition sync_daily / sync_financial ...
├─ screener    screen_stocks / screener_status ...
└─ position    pm_create_portfolio / pm_execute_trade / pm_get_buying_advice ...
```

## 2. 核心用法与注意事项

### 2.1 启动

```bash
# 默认 http
python MCPserver.py

# stdio（给本地 LLM 客户端）
TRANSPORT=stdio python MCPserver.py
```

配置来自 `config.json` 的 `server` 段：`name` / `transport` / `host` / `port`。

### 2.2 新增一个工具

```python
# data_interface/api/market_tools.py
from .utils import ok, fail, from_exception

def register_market_tools(mcp):
    @mcp.tool
    def get_my_metric(code: str, start: str, end: str) -> dict:
        """一句话说明（会作为工具描述展示给模型）。"""
        try:
            return ok({"code": code, "value": ...})
        except Exception as exc:
            return from_exception(exc)
```

然后在 `MCPserver.py` 里**无需改动**——只要 `register_market_tools` 已注册。

### 2.3 注意事项（坑）

| 坑 | 说明 |
|---|---|
| **返回值必须可 JSON 序列化** | DataFrame / numpy 类型要走 `dataframe_to_records` / `json_safe`，否则调用方解析失败 |
| **统一用 `ok` / `fail`** | 返回结构不一致会让 LLM 难以处理；错误走 `from_exception` 保留异常类型 |
| **用 runtime 单例** | 不要在每个工具里 `new QuestDBClient()`；一律 `get_*` 工厂，避免连接泄漏 |
| **工具描述要写清楚** | docstring 就是模型看到的说明；参数含义、单位、时区必须写明 |
| **大结果要分页/限流** | 一次返回几万行会拖垮客户端；工具内加 `limit` 并在描述里说明 |
| **传输差异** | `stdio` 下不能有 `print` 到 stdout（会污染协议流），日志走 `setup_logging` |
| **同步工具很慢** | `sync_*` 类工具是分钟级，应该返回"任务已提交"而不是阻塞等待 |
| **持仓工具带 `pm_` 前缀** | 是历史命名，改名会破坏现有客户端配置 |
