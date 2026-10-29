# 分时 / 板块映射 / 披露日历：数据管线说明（P0–P2）

本文记录 2026-10-28 补齐的三块数据能力、口径、限制与运行方式。

---

## 0. 一句话结论

| 能力 | 状态 | 落地表 | 入口 |
|---|---|---|---|
| 分时（1min/5min） | ✅ 可用 | `ohlcv_1m` / `ohlcv_5m` | `scripts/backfill_intraday.py` |
| 个股↔板块映射 + 板块收益自算 | ✅ 可用 | `map_stock_board` / `dim_board` / `concept_board_daily(source='computed.bk')` | `scripts/backfill_boards.py` |
| 预约披露日历 + 公告列表 | ✅ 可用 | `fin_disclosure_calendar` / `announcement` | `scripts/backfill_disclosure.py` |

验收：`python scripts/verify_p0_p2.py`（11 项检查，全绿）。

---

## 1. 分时（P0）

### 源与原因
- 东财 `push2his` / `push2` 的 **kline 接口在本机被直接断连**（3/3 失败），
  任何走 `akshare.stock_zh_a_hist_min_em` 的调用都不可用；
- `baostock` 的分钟查询**挂起 >5 分钟**，会把同步任务卡死；
- 新浪 `quotes.sina.cn` 的 JSONP 接口稳定可用 → 新增 `SinaMinuteSource`。

### 口径
- 写入 `volume` 单位「股」、`amount` 单位「元」（与库内一致，不再 ×100）；
- 5min 响应缺 `amount` 时补 0，并打 `attrs["missing_amount"]=True`；
- **时间口径**：写入时把北京时间墙钟格式化成字符串，QuestDB 以 UTC 标签存储；
  读回时必须「丢时区标签、不做换算」，否则 11:12 的封板会被算成 19:12
  （`data_interface/market/intraday.py` 的 `_to_frame` 已处理）。

### 硬限制
| 项 | 限制 |
|---|---|
| 单次条数 | `datalen ≤ 1023`，超过新浪返回 `null` |
| 历史深度 | 无区间参数，只能拿「截至最新」：`1min ≈ 4 个交易日`、`5min ≈ 21 个交易日` |
| 竞价 | 无 09:25 独立 bar（开盘价 = 首根 bar 的 open）；收盘竞价并入 `15:00` bar |
| 定位 | **每日增量 + 近端复核**，不能替代长历史回补 |

### 封板/竞价指标
`data_interface/market/intraday.py`：

- `limit_prices(prev_close, board, is_st)`：主板 10% / 创业板·科创板 20% / 北交所 30% / ST 5%；
- `session_metrics(df, prev_close, ...)`：首次封板时点、未封板时点、炸板次数、
  封板后最大回撤、尾盘竞价量与占比、均价线偏离、收盘位置、封板质量分（0–100 启发式）。

实测 603699 / 2026-10-28：`first_seal=11:12`、`break_count=2`、`auction_volume=98,200 股`。

---

## 2. 板块映射（P1）

### 命名空间隔离（重要）
- `concept_member_snapshot` / `concept_board_daily` 里的既有数据是**同花顺 3000xx**；
- `map_stock_board` / `dim_board` 走**东财 BKxxxx**；
- 两者**不混写**，靠 `source` 区分（`em.slist` / `computed.bk`）。

### 取数方式
- `push2 /api/qt/slist/get?spt=3`：**一股一请求**拿到全部板块归属（603699 → 16 个）；
  比「按板块拉成分」少 3 个数量级请求；
- `spt=1`（按板块取成分）返回 `rc=102` 不可用；`spt=2` 是「同行业个股」（603699 → 257 只），
  用于让行业板块有完整成分；
- `clist`（板块/成分列表）在本机**被断连**，所以板块类型无法从接口字段判定。

### 板块类型（启发式，已在源码标注）
- `region`：名称以「板块」结尾；
- `index`：`ATTRIBUTE_BOARDS` 白名单（沪股通/融资融券/中证500/中盘成长…）；
- `industry`：`INDUSTRY_BOARDS` 白名单（金属制品/通用设备/专用设备…）；
- 其余 `concept`；
- `is_primary`：取 `spt=3` 返回列表第 1 项（东财把「所属行业」排首位）。

### 板块收益自算
`ConceptService.compute_board_daily()`：
`board_ret = Σ w·r / Σ w`，权重默认**等权**（对照东财口径偏差最小），可切 `float_mv`；
指数以 1000 为基点，写回 `concept_board_daily`、`source='computed.bk'`。

对照：2026-10-28 自算 BK0545 通用设备 **+3.85%**，东财口径 **+3.65%**（偏差 0.20pp）。
等权优于流通市值加权（后者 +4.41%，偏差 0.76pp），原因是东财板块指数并非自由流通市值加权。

---

## 3. 披露日历与公告（P2）

### 源
- 预约披露：`datacenter-web.eastmoney.com` `RPT_PUBLIC_BS_APPOIN`
  （`pageSize=500`，单期 12 页 / 5581 条）；
- 公告：`np-anotice-stock.eastmoney.com/api/security/ann`（按股票分页）；
- **Tushare `disclosure_date` 在本账号（2120 分）返回 0 行，不可用**。

### 表
- `fin_disclosure_calendar`：`ts`（快照日）+ `code` + `report_date` 为粒度，
  保留 `first/second/third_change_date`（改期次数＝财报不确定性信号）、
  `actual_date`、`residual_days`；
- `announcement`：`art_code` 为业务主键，含 `column_name`（公告类型，如「股份质押、冻结」）。

### 注意
QuestDB 的 `LATEST ON ... PARTITION BY` **后面不能直接跟 `WHERE`**，必须包一层子查询；
且语义上也要「先取最新快照，再过滤」（否则已披露的公司会被旧快照的 `actual_date IS NULL` 捞出来）。

---

## 4. 日常运行

```bash
# 每日收盘后（分时：近端增量，幂等）
python scripts/backfill_intraday.py --codes 603699 --freq 5min
python scripts/backfill_intraday.py --codes 603699 --freq 1min

# 板块映射（一批股票）+ 自算板块日线
python scripts/backfill_boards.py --codes 603699 --peers --returns --start 2026-09-01

# 披露日历（当期）+ 公告（latest）
python scripts/backfill_disclosure.py --calendar --report-date 2026-09-30 --codes 603699

# 验收
python scripts/verify_p0_p2.py
```

### 规模参考
| 表 | 当前规模 |
|---|---|
| `ohlcv_1m` | 238 行（603699，1 日） |
| `ohlcv_5m` | 1,023 行（603699，21 日） |
| `map_stock_board` | 3,615 行 / 227 只 / 357 板块 |
| `fin_disclosure_calendar` | 5,581 行（2026Q3 全市场） |
| `announcement` | 300 行（603699，2024-11 → 2026-10） |

---

## 5. 已知限制与后续

1. **分时历史深度不足**：新浪只给近端；如需 3 年 5min，需要另找源或自建落库（用户后续自行补足）。
2. **板块类型是启发式**：`clist` 恢复后可换成权威分类，只需替换 `classify_board`。
3. **`spt=3` 一股一请求**：全市场约 5576 次、约 20 分钟；建议按池子分批。
4. **公告未做全市场**：`--codes` 指定；若要做事件因子，需按报告期或按池子批量拉。
5. **`residual_days` 是数据源给的**：跨日复算时应自行用 `appoint_date - today` 重算。
