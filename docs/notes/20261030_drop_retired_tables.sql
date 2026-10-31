-- FinBridge 数据库清理 · 2026-10-30
-- 删除 5 张已退役空表（其中 3 张旧概念表仅剩无调用点的 legacy 方法）；QuestDB 的 DROP TABLE 不可恢复，本文件用于回滚重建。
-- 用法：逐条 POST 到 http://127.0.0.1:9000/exec 或 psql -p 8812 -U admin -d qdb -f <file>
-- 说明：SHOW CREATE TABLE 输出的标识符已改为双引号，结构与原表一致（回滚后无数据）。

-- dropped: concept_boards (rows=0, at 2026-10-30)
CREATE TABLE "concept_boards" ( 
	ts TIMESTAMP,
	code SYMBOL,
	name STRING
) timestamp(ts) PARTITION BY YEAR
DEDUP UPSERT KEYS(ts,code);

-- dropped: concept_boards_ohlc (rows=0, at 2026-10-30)
CREATE TABLE "concept_boards_ohlc" ( 
	ts TIMESTAMP,
	code SYMBOL,
	name STRING,
	open DOUBLE,
	high DOUBLE,
	low DOUBLE,
	close DOUBLE,
	volume DOUBLE,
	amount DOUBLE
) timestamp(ts) PARTITION BY MONTH
DEDUP UPSERT KEYS(ts,code);

-- dropped: concept_constituents (rows=0, at 2026-10-30)
CREATE TABLE "concept_constituents" ( 
	ts TIMESTAMP,
	board_name STRING,
	board_code SYMBOL,
	stock_name STRING,
	stock_code SYMBOL
) timestamp(ts) PARTITION BY YEAR
DEDUP UPSERT KEYS(ts,board_code,stock_code);

-- dropped: financial_indicators (rows=0, at 2026-10-30)
CREATE TABLE "financial_indicators" ( 
	ts TIMESTAMP,
	code SYMBOL,
	statDate STRING,
	pubDate STRING,
	roeAvg DOUBLE,
	npMargin DOUBLE,
	gpMargin DOUBLE,
	netProfit DOUBLE,
	epsTTM DOUBLE,
	MBRevenue DOUBLE,
	totalShare DOUBLE,
	liqaShare DOUBLE
) timestamp(ts) PARTITION BY MONTH
DEDUP UPSERT KEYS(ts,code);

-- dropped: stock_industry (rows=0, at 2026-10-30)
CREATE TABLE "stock_industry" ( 
	ts TIMESTAMP,
	code SYMBOL,
	industry_name STRING,
	industry_classification STRING
) timestamp(ts) PARTITION BY MONTH
DEDUP UPSERT KEYS(ts,code);
