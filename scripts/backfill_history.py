#!/usr/bin/env python
"""历史数据补齐（P0-P3）。

阶段
----
- ``p0-codefix``   修复 5 只与指数同码的个股（000001/000016/000688/000852/000905）
- ``p0-daily``     按交易日补全市场日线（含 adj_factor / turnover）
- ``p1-delisted``  补退市股历史行情
- ``p1-index``     补历史指数成分（index_weight）
- ``p2-structure`` 补结构数据（龙虎榜/两融/大宗/解禁/股东户数/回购）
- ``p3-financial`` 补财务三表 + 指标（按代码，默认 2010 起）

设计
----
- **按日拉取**：``daily(trade_date=...)`` 一次拿全市场，比按代码快 5 倍；
- **可中断**：每个阶段按日期/代码游标跳过已完成部分，重跑幂等；
- **单位换算**：Tushare 的 vol（手）×100 → 股，amount（千元）×1000 → 元；
- **写入口径**：复用 ``MarketDataFetcher.update_ohlc``，保证列顺序/复权/market 一致。

用法::

    python scripts/backfill_history.py --phase p0-codefix
    python scripts/backfill_history.py --phase p0-daily --start 2019-01-01 --end 2022-10-09
    python scripts/backfill_history.py --phase p1-delisted
    python scripts/backfill_history.py --phase p1-index
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from data_interface.common.config import get_config, resolve_questdb_host  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402

logger = logging.getLogger("Scripts.BackfillHistory")

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"

#: 与指数同码的个股（ts_code 后缀按交易所）
#: 需要补历史成分的指数（Tushare index_code）
INDEX_CODES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
    "000001.SH": "上证指数",
    "399006.SZ": "创业板指",
}

INDEX_WEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS index_weight (
    ts TIMESTAMP,
    index_code SYMBOL,
    code SYMBOL,
    weight DOUBLE
) TIMESTAMP(ts) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(ts, index_code, code)
"""

SAME_CODE_STOCKS = {
    "000001": "000001.SZ",   # 平安银行
    "000016": "000016.SZ",   # 深康佳A
    "000688": "000688.SZ",   # 国城矿业
    "000852": "000852.SZ",   # 石化机械
    "000905": "000905.SZ",   # 厦门港务
}


# ---------------------------------------------------------------- 基础设施
def _sql(sql: str, timeout: int = 120, retries: int = 5):
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
            if resp.status_code != 200 or not resp.text.strip():
                last = f"HTTP {resp.status_code} 空响应"
                time.sleep(3 * (attempt + 1))
                continue
            payload = resp.json()
            if "error" in payload:
                raise RuntimeError(f"{payload['error']} :: {sql[:160]}")
            return payload.get("dataset") or []
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:120]
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"QuestDB 查询失败（{last}）:: {sql[:160]}")


def _tushare():
    import tushare as ts

    token = _cfg.get("tushare", {}).get("token")
    if not token:
        raise RuntimeError("config.json 里没有 tushare.token")
    return ts.pro_api(token)


class _Api:
    """带重试与限速的 Tushare 包装。"""

    def __init__(self, pro, sleep: float = 0.12, retries: int = 3):
        self.pro = pro
        self.sleep = sleep
        self.retries = retries
        self.calls = 0

    def call(self, name: str, **kwargs) -> pd.DataFrame | None:
        for attempt in range(self.retries):
            try:
                frame = getattr(self.pro, name)(**kwargs)
                self.calls += 1
                time.sleep(self.sleep)
                return frame
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)[:120]
                if "频率超限" in msg or "每分钟" in msg:
                    wait = 60
                elif attempt < self.retries - 1:
                    wait = 2 * (attempt + 1)
                else:
                    logger.warning("%s 失败（%s）：%s", name, kwargs, msg)
                    return None
                logger.info("%s 限频/异常，等待 %ss 后重试", name, wait)
                time.sleep(wait)
        return None


def _write_ohlc(frame: pd.DataFrame) -> int:
    """写 ohlcv_1d；先清洗数值，单批失败只记日志不中断。

    QuestDB 的 volume 是 LONG，NaN 会被 psycopg2 以 ``NaN`` 文本发送并导致整批
    插入失败；退市股常出现空成交量，所以这里统一清洗。
    """
    if frame is None or frame.empty:
        return 0
    frame = frame.copy()
    for column in ("open", "high", "low", "close", "amount", "turnover", "adj_factor"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return 0
    frame["volume"] = (
        pd.to_numeric(frame["volume"], errors="coerce").fillna(0).clip(lower=0).astype("int64")
    )
    frame["amount"] = frame["amount"].fillna(0.0)
    frame["turnover"] = frame["turnover"].where(frame["turnover"].notna(), None)

    from data_interface.market.fetcher import MarketDataFetcher

    fetcher = MarketDataFetcher()
    try:
        return fetcher.update_ohlc(frame, "d")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入失败（%s 行，跳过）：%s", len(frame), str(exc)[:120])
        return 0
    finally:
        fetcher.close()


def _existing_counts(dates: list[str]) -> dict[str, int]:
    """每个交易日已有个股行数（用于跳过）。"""
    if not dates:
        return {}
    start, end = min(dates), max(dates)
    try:
        rows = _sql(
            "select ts, count() from ohlcv_1d "
            f"where market='MLCN' and ts between '{start}' and '{end}' group by ts",
            timeout=300,
        )
        return {str(r[0])[:10]: int(r[1]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("覆盖度查询失败（按全量重跑处理）：%s", str(exc)[:90])
        return {}


def _trading_days(start: str, end: str) -> list[str]:
    """交易日取 ``trading_days`` 表（比 distinct ohlcv_1d 快几个数量级）。"""
    try:
        rows = _sql(
            "select ts from trading_days where is_trading = true "
            f"and ts between '{start}' and '{end}' order by ts"
        )
        if rows:
            return [str(r[0])[:10] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("trading_days 查询失败，改用 Tushare 日历：%s", str(exc)[:80])
    return []


# ---------------------------------------------------------------- P0：同码股
def phase_codefix(api: _Api, args) -> int:
    logger.info("P0-a 修复 %s 只同码股", len(SAME_CODE_STOCKS))
    total = 0
    for code, ts_code in SAME_CODE_STOCKS.items():
        frame = api.call(
            "daily", ts_code=ts_code, start_date="19910101", end_date="20261231"
        )
        if frame is None or frame.empty:
            logger.warning("  %s 无数据", ts_code)
            continue
        frame = frame.sort_values("trade_date")
        adj = api.call("adj_factor", ts_code=ts_code, start_date="19910101",
                       end_date="20261231")
        adj_map = {}
        if adj is not None and not adj.empty:
            adj_map = dict(zip(adj["trade_date"], adj["adj_factor"]))

        out = pd.DataFrame(
            {
                "ts": pd.to_datetime(frame["trade_date"], format="%Y%m%d"),
                "open": pd.to_numeric(frame["open"], errors="coerce"),
                "high": pd.to_numeric(frame["high"], errors="coerce"),
                "low": pd.to_numeric(frame["low"], errors="coerce"),
                "close": pd.to_numeric(frame["close"], errors="coerce"),
                "volume": pd.to_numeric(frame["vol"], errors="coerce") * 100,
                "amount": pd.to_numeric(frame["amount"], errors="coerce") * 1000,
                "turnover": pd.NA,
                "adj_factor": frame["trade_date"].map(adj_map),
                "code": code,
            }
        )
        written = _write_ohlc(out)
        total += written
        logger.info("  %s(%s): %s 行 → 写入 %s", code, ts_code, len(out), written)
    logger.info("P0-a 完成，共写入 %s 行", total)
    return 0


# ---------------------------------------------------------------- P0：日线
def phase_daily(api: _Api, args) -> int:
    dates = _trading_days(args.start, args.end)
    if not dates:
        # 目标区间在库里没有交易日（例如整段缺失）：用交易日历兜底
        cal = api.call("trade_cal", exchange="SSE", start_date=args.start.replace("-", ""),
                       end_date=args.end.replace("-", ""))
        if cal is None or cal.empty:
            logger.error("取不到交易日历，退出")
            return 1
        dates = [
            datetime.strptime(str(r), "%Y%m%d").strftime("%Y-%m-%d")
            for r, flag in zip(cal["cal_date"], cal["is_open"])
            if str(flag) == "1"
        ]
    existing = _existing_counts(dates)
    todo = [d for d in dates if existing.get(d, 0) < args.min_codes]
    logger.info(
        "P0-b 目标 %s 个交易日，已有完整 %s 天，待补 %s 天（阈值 %s 只）",
        len(dates), len(dates) - len(todo), len(todo), args.min_codes,
    )
    if not todo:
        return 0

    total = 0
    started = time.time()
    for index, day in enumerate(todo, 1):
        compact = day.replace("-", "")
        daily = api.call("daily", trade_date=compact)
        if daily is None or daily.empty:
            logger.warning("  %s 无行情", day)
            continue
        adj = api.call("adj_factor", trade_date=compact)
        basic = api.call("daily_basic", trade_date=compact,
                         fields="ts_code,trade_date,turnover_rate,float_share")

        adj_map = dict(zip(adj["ts_code"], adj["adj_factor"])) if adj is not None and not adj.empty else {}
        basic_map = (
            dict(zip(basic["ts_code"], basic["turnover_rate"]))
            if basic is not None and not basic.empty
            else {}
        )

        frame = pd.DataFrame(
            {
                "ts": pd.to_datetime(daily["trade_date"], format="%Y%m%d"),
                "open": pd.to_numeric(daily["open"], errors="coerce"),
                "high": pd.to_numeric(daily["high"], errors="coerce"),
                "low": pd.to_numeric(daily["low"], errors="coerce"),
                "close": pd.to_numeric(daily["close"], errors="coerce"),
                "volume": pd.to_numeric(daily["vol"], errors="coerce") * 100,
                "amount": pd.to_numeric(daily["amount"], errors="coerce") * 1000,
                "turnover": daily["ts_code"].map(basic_map),
                "adj_factor": daily["ts_code"].map(adj_map),
                "code": daily["ts_code"].str.slice(0, 6),
            }
        )
        frame = frame.dropna(subset=["open", "close"])
        total += _write_ohlc(frame)
        if index % 20 == 0 or index == len(todo):
            elapsed = time.time() - started
            rate = index / max(elapsed, 1e-9)
            eta = (len(todo) - index) / max(rate, 1e-9) / 60
            logger.info(
                "  [%s/%s] %s 写入累计 %s 行 | %.1f 天/分钟 | 剩余约 %.0f 分钟",
                index, len(todo), day, f"{total:,}", rate * 60, eta,
            )
    logger.info("P0-b 完成：写入 %s 行", f"{total:,}")
    return 0


# ---------------------------------------------------------------- P1：退市股
def phase_delisted(api: _Api, args) -> int:
    """补退市股历史行情（消除幸存者偏差）。"""
    basic = api.call(
        "stock_basic", list_status="D",
        fields="ts_code,symbol,name,list_date,delist_date",
    )
    if basic is None or basic.empty:
        logger.error("取不到退市股清单")
        return 1
    logger.info("P1-a 退市股 %s 只", len(basic))

    total = 0
    for index, row in enumerate(basic.itertuples(index=False), 1):
        ts_code = row.ts_code
        start = str(row.list_date or "19910101")
        end = str(row.delist_date or "20261231")
        frame = api.call("daily", ts_code=ts_code, start_date=start, end_date=end)
        if frame is None or frame.empty:
            continue
        adj = api.call("adj_factor", ts_code=ts_code, start_date=start, end_date=end)
        adj_map = (
            dict(zip(adj["trade_date"], adj["adj_factor"]))
            if adj is not None and not adj.empty
            else {}
        )
        out = pd.DataFrame(
            {
                "ts": pd.to_datetime(frame["trade_date"], format="%Y%m%d"),
                "open": pd.to_numeric(frame["open"], errors="coerce"),
                "high": pd.to_numeric(frame["high"], errors="coerce"),
                "low": pd.to_numeric(frame["low"], errors="coerce"),
                "close": pd.to_numeric(frame["close"], errors="coerce"),
                "volume": pd.to_numeric(frame["vol"], errors="coerce") * 100,
                "amount": pd.to_numeric(frame["amount"], errors="coerce") * 1000,
                "turnover": pd.NA,
                "adj_factor": frame["trade_date"].map(adj_map),
                "code": str(row.symbol),
            }
        )
        out = out.dropna(subset=["open", "close"])
        total += _write_ohlc(out)
        if index % 20 == 0 or index == len(basic):
            logger.info("  [%s/%s] 累计 %s 行", index, len(basic), f"{total:,}")
    logger.info("P1-a 完成：退市股写入 %s 行", f"{total:,}")
    return 0


# ---------------------------------------------------------------- P1：指数成分
def phase_index(api: _Api, args) -> int:
    """补历史指数成分（按半年切块拉 index_weight）。"""
    _sql(INDEX_WEIGHT_DDL)
    total = 0
    # 续跑：已有足够数据的指数直接跳过
    done = {
        str(r[0]): int(r[1])
        for r in (_sql("select index_code, count() from index_weight group by index_code") or [])
    }
    for index_code, name in INDEX_CODES.items():
        if done.get(index_code, 0) > 1000:
            logger.info("  %s(%s) 已有 %s 行，跳过", name, index_code, f"{done[index_code]:,}")
            continue
        chunks = []
        year, month = 2010, 1
        while (year, month) <= (2026, 12):
            start = f"{year}{month:02d}01"
            end_month = month + 5
            end_year = year + (end_month - 1) // 12
            end_month = (end_month - 1) % 12 + 1
            end = f"{end_year}{end_month:02d}28"
            chunks.append((start, end))
            month += 6
            if month > 12:
                month -= 12
                year += 1
        written = 0
        for start, end in chunks:
            frame = api.call(
                "index_weight", index_code=index_code,
                start_date=start, end_date=end,
            )
            if frame is None or frame.empty:
                continue
            rows = []
            for r in frame.itertuples(index=False):
                weight = pd.to_numeric(getattr(r, "weight", None), errors="coerce")
                if weight != weight:  # NaN
                    continue
                rows.append(
                    (
                        pd.Timestamp(str(r.trade_date)).to_pydatetime(),
                        index_code,
                        str(r.con_code)[:6],
                        float(weight),
                    )
                )
            try:
                written += _bulk_insert(
                    "index_weight",
                    ["ts", "index_code", "code", "weight"],
                    rows,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("  %s %s~%s 写入失败：%s", index_code, start, end, str(exc)[:90])
        total += written
        logger.info("  %s(%s): %s 行", name, index_code, f"{written:,}")
    logger.info("P1-b 完成：index_weight 写入 %s 行", f"{total:,}")
    return 0


def _bulk_insert(table: str, columns: list[str], rows: list[tuple]) -> int:
    """分批 INSERT（每 5 万行一冲），返回写入行数。"""
    if not rows:
        return 0
    db = QuestDBClient()
    quoted = ", ".join(f'"{c}"' for c in columns)
    prefix = f'INSERT INTO "{table}" ({quoted}) VALUES %s'
    written = 0
    for offset in range(0, len(rows), 50_000):
        written += int(db.execute_values(prefix, rows[offset:offset + 50_000]) or 0)
    return written


#: 结构数据表（全部按年分区 + dedup，幂等重跑）
STRUCTURE_TABLES = {
    "top_list": (
        """CREATE TABLE IF NOT EXISTS top_list (
            ts TIMESTAMP, code SYMBOL, name VARCHAR, close DOUBLE, pct_chg DOUBLE,
            turnover_rate DOUBLE, amount DOUBLE, l_sell DOUBLE, l_buy DOUBLE,
            l_amount DOUBLE, net_amount DOUBLE, reason VARCHAR
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code)""",
        ["ts", "code", "name", "close", "pct_chg", "turnover_rate", "amount",
         "l_sell", "l_buy", "l_amount", "net_amount", "reason"],
    ),
    "margin_detail": (
        """CREATE TABLE IF NOT EXISTS margin_detail (
            ts TIMESTAMP, code SYMBOL, rzye DOUBLE, rqye DOUBLE, rzmre DOUBLE,
            rqyl DOUBLE, rzche DOUBLE, rqchl DOUBLE
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code)""",
        ["ts", "code", "rzye", "rqye", "rzmre", "rqyl", "rzche", "rqchl"],
    ),
    "block_trade": (
        """CREATE TABLE IF NOT EXISTS block_trade (
            ts TIMESTAMP, code SYMBOL, price DOUBLE, vol DOUBLE, amount DOUBLE,
            buyer VARCHAR, seller VARCHAR
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code, buyer)""",
        ["ts", "code", "price", "vol", "amount", "buyer", "seller"],
    ),
    "share_float": (
        """CREATE TABLE IF NOT EXISTS share_float (
            ts TIMESTAMP, code SYMBOL, float_date TIMESTAMP, float_share DOUBLE,
            float_ratio DOUBLE, holder_name VARCHAR, share_type VARCHAR
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code, float_date)""",
        ["ts", "code", "float_date", "float_share", "float_ratio", "holder_name", "share_type"],
    ),
    "stk_holdernumber": (
        """CREATE TABLE IF NOT EXISTS stk_holdernumber (
            ts TIMESTAMP, code SYMBOL, end_date TIMESTAMP, holder_num LONG
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code)""",
        ["ts", "code", "end_date", "holder_num"],
    ),
    "repurchase": (
        """CREATE TABLE IF NOT EXISTS repurchase (
            ts TIMESTAMP, code SYMBOL, end_date TIMESTAMP, proc VARCHAR, exp_date TIMESTAMP,
            vol DOUBLE, amount DOUBLE, high_limit DOUBLE, low_limit DOUBLE
        ) TIMESTAMP(ts) PARTITION BY YEAR WAL DEDUP UPSERT KEYS(ts, code, end_date)""",
        ["ts", "code", "end_date", "proc", "exp_date", "vol", "amount",
         "high_limit", "low_limit"],
    ),
}


# ---------------------------------------------------------------- P2：结构数据
def _native(value):
    """转成 psycopg2 能直接绑定的原生值。

    注意：**只对 datetime 做转换**，绝不把普通字符串当日期解析——
    ``'000001'`` 会被 pandas 解析成 2000-01-01，导致 TIMESTAMP → SYMBOL 类型错误。
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).to_pydatetime()
    return str(value)[:200]


def _insert_struct(table: str, columns: list[str], records: list[dict]) -> int:
    rows = [tuple(_native(rec.get(col)) for col in columns) for rec in records]
    return _bulk_insert(table, columns, rows)


def phase_structure(api: _Api, args) -> int:
    """补 6 类结构数据：龙虎榜/两融/大宗/解禁/股东户数/回购。"""
    for ddl, _cols in STRUCTURE_TABLES.values():
        _sql(ddl)

    days = _trading_days(args.start, args.end)
    logger.info("P2 交易日 %s 天（%s ~ %s）", len(days), args.start, args.end)

    total = 0
    started = time.time()
    for index, day in enumerate(days, 1):
        compact = day.replace("-", "")
        ts_literal = pd.Timestamp(day).to_pydatetime()

        top = api.call("top_list", trade_date=compact)
        if top is not None and not top.empty:
            total += _insert_struct(
                "top_list",
                STRUCTURE_TABLES["top_list"][1],
                [
                    {
                        "ts": ts_literal, "code": str(r.ts_code)[:6], "name": r.name,
                        "close": r.close, "pct_chg": r.pct_change,
                        "turnover_rate": r.turnover_rate, "amount": r.amount,
                        "l_sell": r.l_sell, "l_buy": r.l_buy, "l_amount": r.l_amount,
                        "net_amount": r.net_amount, "reason": r.reason,
                    }
                    for r in top.itertuples(index=False)
                ],
            )

        margin = api.call("margin_detail", trade_date=compact)
        if margin is not None and not margin.empty:
            total += _insert_struct(
                "margin_detail",
                STRUCTURE_TABLES["margin_detail"][1],
                [
                    {
                        "ts": ts_literal, "code": str(r.ts_code)[:6], "rzye": r.rzye,
                        "rqye": r.rqye, "rzmre": r.rzmre, "rqyl": r.rqyl,
                        "rzche": r.rzche, "rqchl": r.rqchl,
                    }
                    for r in margin.itertuples(index=False)
                ],
            )

        block = api.call("block_trade", trade_date=compact)
        if block is not None and not block.empty:
            total += _insert_struct(
                "block_trade",
                STRUCTURE_TABLES["block_trade"][1],
                [
                    {
                        "ts": ts_literal, "code": str(r.ts_code)[:6], "price": r.price,
                        "vol": r.vol, "amount": r.amount, "buyer": r.buyer,
                        "seller": r.seller,
                    }
                    for r in block.itertuples(index=False)
                ],
            )

        if index % 20 == 0 or index == len(days):
            elapsed = time.time() - started
            eta = (len(days) - index) * elapsed / index / 60
            logger.info(
                "  [%s/%s] %s 累计 %s 行 | 剩余约 %.0f 分钟",
                index, len(days), day, f"{total:,}", eta,
            )

    # 区间类数据（一次拉全区间）
    for name, fn, cols, mapper in (
        ("share_float", "share_float", STRUCTURE_TABLES["share_float"][1], None),
        ("repurchase", "repurchase", STRUCTURE_TABLES["repurchase"][1], None),
    ):
        frame = api.call(fn, start_date=args.start.replace("-", ""),
                         end_date=args.end.replace("-", ""))
        if frame is None or frame.empty:
            logger.warning("  %s 无数据", name)
            continue
        records = []
        for r in frame.itertuples(index=False):
            base = {"ts": pd.Timestamp(str(r.ann_date)).to_pydatetime(),
                    "code": str(r.ts_code)[:6]}
            if name == "share_float":
                base.update(
                    float_date=pd.Timestamp(str(r.float_date)).to_pydatetime(),
                    float_share=r.float_share, float_ratio=r.float_ratio,
                    holder_name=r.holder_name, share_type=r.share_type,
                )
            else:
                base.update(
                    end_date=(pd.Timestamp(str(r.end_date)).to_pydatetime()
                              if r.end_date else None),
                    proc=r.proc,
                    exp_date=(pd.Timestamp(str(r.exp_date)).to_pydatetime()
                              if r.exp_date else None),
                    vol=r.vol, amount=r.amount,
                    high_limit=getattr(r, "high_limit", None),
                    low_limit=getattr(r, "low_limit", None),
                )
            records.append(base)
        written = _insert_struct(name, cols, records)
        total += written
        logger.info("  %s: %s 行", name, f"{written:,}")

    logger.info("P2 完成：结构数据写入 %s 行", f"{total:,}")
    return 0


# ---------------------------------------------------------------- P3：财务
def phase_financial(api: _Api, args) -> int:
    """补财务三表 + 指标（按代码；默认 2010 起）。"""
    from data_interface.financial.service import FinancialDataService

    service = FinancialDataService()
    # 只补「有足够日线」的代码：长期停牌/退市前的空壳没有报表价值
    min_days = int(getattr(args, "min_days", 0) or 0)
    if min_days > 0:
        rows = _sql(
            "select code from (select code, count() n from ohlcv_1d where market='MLCN' "
            "group by code) where n >= " + str(min_days) + " order by code"
        )
    else:
        rows = _sql(
            "select distinct code from ohlcv_1d where market='MLCN' order by code"
        )
    codes = [str(r[0]) for r in rows]
    logger.info("P3 财务：%s 只，起始年 %s", len(codes), args.start_year)

    # 已有 2010 年前报表的代码跳过（续跑）
    have_old = {
        str(r[0])
        for r in (_sql("select distinct code from fin_income_statement "
                       "where period_end < '2015-01-01'") or [])
    }
    if have_old:
        codes = [c for c in codes if c not in have_old]
        logger.info("  已有 2015 前报表 %s 只，跳过；剩余 %s 只", len(have_old), len(codes))

    shards = max(1, int(getattr(args, "shards", 1)))
    shard = int(getattr(args, "shard", 0))
    if shards > 1:
        codes = [c for i, c in enumerate(codes) if i % shards == shard]
        logger.info("  分片 %s/%s：本片 %s 只", shard + 1, shards, len(codes))

    done = 0
    failed = 0
    started = time.time()
    for index, code in enumerate(codes, 1):
        try:
            # 只同步三表（profile/valuation 已由日常同步维护，且 AkShare 接口不稳定）
            service.sync_statements(
                code, start_year=args.start_year, end_year=2026, force=True
            )
            done += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                logger.warning("  %s 失败：%s", code, str(exc)[:100])
        if index % 25 == 0 or index == len(codes):
            elapsed = time.time() - started
            eta = (len(codes) - index) * elapsed / index / 3600
            logger.info(
                "  [%s/%s] 成功 %s 失败 %s | 剩余约 %.1f 小时",
                index, len(codes), done, failed, eta,
            )
    logger.info("P3 完成：成功 %s，失败 %s", done, failed)
    return 0


# ---------------------------------------------------------------- P2 补：区间类分页
def _month_chunks(start: str, end: str) -> list[tuple[str, str]]:
    begin = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    chunks = []
    while begin <= stop:
        nxt = (begin + pd.offsets.MonthEnd(0))
        chunks.append((begin.strftime("%Y%m%d"), min(nxt, stop).strftime("%Y%m%d")))
        begin = nxt + pd.Timedelta(days=1)
    return chunks


def phase_structure_periodic(api: _Api, args) -> int:
    """按月分页拉 share_float / repurchase / stk_holdernumber。"""
    for key in ("share_float", "repurchase", "stk_holdernumber"):
        _sql(STRUCTURE_TABLES[key][0])

    chunks = _month_chunks(args.start, args.end)
    logger.info("P2-c 区间类数据：%s 个月", len(chunks))
    total = 0

    for index, (start, end) in enumerate(chunks, 1):
        # 解禁
        frame = api.call("share_float", start_date=start, end_date=end)
        if frame is not None and not frame.empty:
            records = [
                {
                    "ts": pd.Timestamp(str(r.ann_date)).to_pydatetime(),
                    "code": str(r.ts_code)[:6],
                    "float_date": pd.Timestamp(str(r.float_date)).to_pydatetime(),
                    "float_share": r.float_share,
                    "float_ratio": r.float_ratio,
                    "holder_name": r.holder_name,
                    "share_type": r.share_type,
                }
                for r in frame.itertuples(index=False)
            ]
            total += _insert_struct("share_float", STRUCTURE_TABLES["share_float"][1], records)

        # 回购
        frame = api.call("repurchase", start_date=start, end_date=end)
        if frame is not None and not frame.empty:
            records = []
            for r in frame.itertuples(index=False):
                records.append(
                    {
                        "ts": pd.Timestamp(str(r.ann_date)).to_pydatetime(),
                        "code": str(r.ts_code)[:6],
                        "end_date": (pd.Timestamp(str(r.end_date)).to_pydatetime()
                                     if r.end_date else None),
                        "proc": r.proc,
                        "exp_date": (pd.Timestamp(str(r.exp_date)).to_pydatetime()
                                     if r.exp_date else None),
                        "vol": r.vol,
                        "amount": r.amount,
                        "high_limit": getattr(r, "high_limit", None),
                        "low_limit": getattr(r, "low_limit", None),
                    }
                )
            total += _insert_struct("repurchase", STRUCTURE_TABLES["repurchase"][1], records)

        # 股东户数
        frame = api.call("stk_holdernumber", start_date=start, end_date=end)
        if frame is not None and not frame.empty:
            records = [
                {
                    "ts": pd.Timestamp(str(r.ann_date)).to_pydatetime(),
                    "code": str(r.ts_code)[:6],
                    "end_date": pd.Timestamp(str(r.end_date)).to_pydatetime(),
                    "holder_num": r.holder_num,
                }
                for r in frame.itertuples(index=False)
            ]
            total += _insert_struct(
                "stk_holdernumber", STRUCTURE_TABLES["stk_holdernumber"][1], records
            )

        if index % 12 == 0 or index == len(chunks):
            logger.info("  [%s/%s] %s 累计 %s 行", index, len(chunks), start, f"{total:,}")

    logger.info("P2-c 完成：写入 %s 行", f"{total:,}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="历史数据补齐")
    parser.add_argument(
        "--phase",
        required=True,
        choices=("p0-codefix", "p0-daily", "p1-delisted", "p1-index",
                 "p2-structure", "p2-periodic", "p3-financial"),
    )
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2022-10-09")
    parser.add_argument("--min-codes", type=int, default=3000,
                        help="当日已有行数达到该值则跳过")
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--sleep", type=float, default=0.12)
    parser.add_argument("--min-days", type=int, default=0, help="日线行数下限（过滤空壳）")
    parser.add_argument("--shard", type=int, default=0, help="分片序号（0 起）")
    parser.add_argument("--shards", type=int, default=1, help="分片总数")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    api = _Api(_tushare(), sleep=args.sleep)

    handlers = {
        "p0-codefix": phase_codefix,
        "p0-daily": phase_daily,
        "p1-delisted": phase_delisted,
        "p1-index": phase_index,
        "p2-structure": phase_structure,
        "p2-periodic": phase_structure_periodic,
        "p3-financial": phase_financial,
    }
    handler = handlers.get(args.phase)
    if handler is None:
        logger.error("阶段 %s 尚未实现", args.phase)
        return 2
    return handler(api, args)


if __name__ == "__main__":
    raise SystemExit(main())
