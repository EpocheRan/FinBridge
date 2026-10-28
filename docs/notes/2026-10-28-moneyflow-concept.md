# 2026-10-28 分档与概念板块数据管线

## 背景

新增两类数据：个股日频分档（超大/大/中/小单 × 主动买卖）与概念板块（列表/日线/成分/分类），
并回补 2022-10-10 ~ 2026-10-27 历史数据。

## 本次改动

### 分档 moneyflow

- 表：`moneyflow_daily`、`moneyflow_source`
- 源：Tushare `moneyflow`，按交易日一次全市场；万元→元、手→股
- 代码：
  - `data_interface/moneyflow/sources/tushare_source.py`
  - `data_interface/moneyflow/{models,repository,service}.py`
- MCP：`get_moneyflow`、`get_moneyflow_rank`、`get_moneyflow_sources`
- 同步：`scheduled_sync_a_shares(dataset="moneyflow")`，支持游标、质量标记、空日 pending 重试
- 回补：`scripts/backfill_moneyflow.py`

### 概念板块 concept

- 表：`concept_board_dim`、`concept_board_daily`、`concept_member_snapshot`、
  `concept_classification`、`concept_board_alias`
- 源：
  - 同花顺（AkShare）：板块列表 375 个、板块日线
  - 同花顺网页：成分兜底（东财优先，本机东财连接被断）
  - Tushare `concept`：概念分类树
- 代码：
  - `data_interface/concept/sources/{ths_source,ths_scrape_source,em_source,tushare_classification}.py`
  - `data_interface/concept/{models,repository,service}.py`
  - `data_interface/concept/sources/ths_fast.py`：年度 K 线文件快速通道，单板块约 0.3~2 秒
- MCP：`get_concept_boards`、`get_concept_members`、`get_concept_ohlc`、`get_concept_classification`
- 同步：`dataset="concept_meta|concept_daily|concept_member|concept_class"`
- 回补：`scripts/backfill_concept.py --dataset dim|daily`

### 其他

- `scheduled_tasks` 新增上述数据集，`status()` 增加对应游标
- `config.example.json` 增加 `moneyflow` / `concept` 配置段
- README 增加回补章节与工具清单
- 设计文档：`docs/design/concept_moneyflow_v1.md`

## 回补结果

| 数据 | 行数 | 区间 |
|---|---|---|
| `moneyflow_daily` | 5,014,483 | 2022-10-10 ~ 2026-10-27，982/982 交易日 |
| `concept_board_daily` | 337,520 | 2022-10-10 ~ 2026-10-27，375 个板块 |
| `concept_board_dim` | 1,125 | 3 个快照（2022-10-10 / 2026-10-27 重建，2026-10-28 真实） |

分档每日 4,825~5,577 只，quality 全部 `ok`，source 全部 `tushare.moneyflow`。

## 已知限制

1. 概念板块没有历史列表/成分源：`dim` 的 2022-10-10 与 2026-10-27 是当前 375 个板块的投影，
   存在幸存者偏差；2022-10-10 实际有日线的板块只有 284 个。
2. 概念分类 `concept` 接口限 1 次/小时，当前表为空，等窗口重跑
   `scheduled_sync_a_shares(dataset="concept_class")`。
3. 成分股只能从接入日开始积累快照，无法回补历史。
4. 分钟线、周/月线仍无批量回补入口。

## 测试

```text
pytest: 310 passed, 12 skipped
```

新增测试覆盖：单位换算、代码规范化、空数据、源回退、游标分批、pending 重试、
质量标记、板块列表/日线、快速年度文件解析、成分 partial 保护、PIT 查询、分类映射、DDL 一致性。
