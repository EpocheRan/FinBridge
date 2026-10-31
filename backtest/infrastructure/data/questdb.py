"""QuestDB 只读数据源。

回测运行期不回源；缺数据由同步任务预补齐。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Sequence

import pandas as pd

from backtest.application.universe import (
    UniverseSchedule,
    explicit_schedule,
    schedule_from_snapshots,
)
from backtest.domain.errors import DataError
from backtest.domain.models import DataBundle, SecurityStatus, TradingCalendar, Universe
from data_interface.common.config import get_config
from data_interface.common.questdb_client import QuestDBClient
from data_interface.market.constants import normalize_frequency, ohlc_table_name

from .snapshot import make_snapshot

logger = logging.getLogger("Backtest.QuestDB")


def _calendar(frame: pd.DataFrame) -> TradingCalendar:
    dates = pd.to_datetime(frame["date"]).dropna().dt.normalize().unique()
    return TradingCalendar(tuple(sorted(pd.Timestamp(item) for item in dates)))


def _with_ts(frame: pd.DataFrame) -> pd.DataFrame:
    """QuestDB 查询把 ts 放在索引上；统一还原成列，供按列取用。"""
    data = frame.reset_index()
    if "ts" not in data.columns:
        data = data.rename(columns={data.columns[0]: "ts"})
    return data


def _ohlc_columns(table: str) -> str:
    """按表返回 OHLCV 列清单。

    分钟线（ohlcv_5m/15m/…)没有 turnover 列；日线/周线有，而换手类因子
    （``liquidity.turnover_std_bias_*``）必须靠它，缺列会退化成全 NaN 信号。
    """
    columns = "open, high, low, close, volume, amount, adj_factor"
    if table in ("ohlcv_1d", "ohlcv_1w"):
        columns = columns.replace("amount,", "amount, turnover,")
    return columns


class QuestDBDataSource:
    name = "questdb"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        db: QuestDBClient | None = None,
        read_only: bool = True,
    ):
        self.config = config or get_config()
        # 数据源构造不连接数据库：横截面/配置错误应在 load 之前失败，
        # 而不是先连一遍 QuestDB 再抛 BacktestError。
        self.db = db or QuestDBClient(self.config, connect=False)
        self.read_only = read_only

    def _load_security_status(self, codes: Sequence[str], end: date) -> SecurityStatus:
        placeholders = ",".join(["%s"] * len(codes))
        sql = (
            "SELECT code, last(is_st) AS is_st, last(list_date) AS list_date, "
            "last(delist_date) AS delist_date FROM stock_basic_info "
            f"WHERE code IN ({placeholders}) GROUP BY code"
        )
        try:
            frame = self.db.query_dataframe(sql, list(codes))
        except Exception as exc:
            logger.warning("证券状态读取失败（按近似处理）: %s", exc)
            return SecurityStatus(approximation=True)
        if frame is None or frame.empty:
            return SecurityStatus(approximation=True)
        current_st: dict[str, bool] = {}
        list_dates: dict[str, date] = {}
        delist_dates: dict[str, date] = {}
        for _, row in frame.iterrows():
            code = str(row.get("code"))
            value = row.get("is_st")
            current_st[code] = bool(value) if pd.notna(value) else False
            for column, target in (
                ("list_date", list_dates),
                ("delist_date", delist_dates),
            ):
                raw = row.get(column)
                if pd.notna(raw):
                    target[code] = pd.Timestamp(raw).date()
        # stock_basic_info 只有当前 is_st；历史 ST 需要 namechange 明细，暂标记近似。
        return SecurityStatus(
            current_st=current_st,
            list_dates=list_dates,
            delist_dates=delist_dates,
            approximation=True,
        )

    def resolve_universe(self, spec, start: date, end: date) -> UniverseSchedule:
        """把 UniverseSpec 解析成「取数代码 + PIT 成分快照」。

        - ``explicit``：原样返回；
        - ``index``：读 ``index_weight``，每期一条快照，按 ts 升序；
          取数代码 = 区间内所有快照的并集（含 start 之前最近一期）；
        - ``all_a``：区间内有行情的全部股票，不做快照过滤（数据可得性即约束）。
        """
        kind = str(getattr(spec, "kind", "explicit")).strip().lower()
        codes = tuple(str(code) for code in getattr(spec, "codes", ()))
        if kind == "explicit":
            return explicit_schedule(codes)
        if kind == "index":
            if not codes:
                raise DataError("index universe 必须给出指数代码，如 Universe.index('000300')")
            return self._resolve_index(codes[0], start, end)
        if kind == "all_a":
            return self._resolve_all_a(
                start, end, exclude_st=bool(getattr(spec, "exclude_st", True))
            )
        raise DataError(f"未知 universe kind: {kind!r}（支持 explicit/index/all_a）")

    def _resolve_index(self, index_code: str, start: date, end: date) -> UniverseSchedule:
        # 多取 500 天，确保能拿到 start 之前最近一期成分
        start_ts = pd.Timestamp(start).normalize() - pd.Timedelta(days=500)
        end_ts = pd.Timestamp(end).normalize()
        candidates = [index_code]
        if "." not in index_code:
            candidates.extend([f"{index_code}.SH", f"{index_code}.SZ"])
        placeholders = ",".join(["%s"] * len(candidates))
        sql = (
            "SELECT ts, code FROM index_weight "
            f"WHERE index_code IN ({placeholders}) AND ts >= %s AND ts <= %s "
            "ORDER BY ts, code"
        )
        try:
            frame = self.db.query_dataframe(sql, [*candidates, start_ts, end_ts])
        except Exception as exc:
            raise DataError(f"指数成分读取失败 {index_code}: {exc}") from exc
        if frame is None or frame.empty:
            raise DataError(
                f"index_weight 没有 {index_code} 的成分（{start}..{end}）；"
                "检查指数代码后缀或先同步 index_weight"
            )
        frame = _with_ts(frame)
        rows = [
            (row["ts"], str(row["code"]))
            for _, row in frame.iterrows()
            if pd.notna(row.get("ts")) and pd.notna(row.get("code"))
        ]
        schedule = schedule_from_snapshots(rows, label=f"index:{index_code}")
        newest = schedule.snapshots[-1].ts
        if newest < end_ts - pd.Timedelta(days=400):
            logger.warning(
                "index %s 的成分快照最新只到 %s，早于回测终点 %s；"
                "该段区间按「冻结成分」处理，结果会失真",
                index_code,
                newest.date(),
                end_ts.date(),
            )
        logger.info(
            "index universe %s: %d 期快照 / %d 只成分",
            index_code,
            len(schedule.snapshots),
            len(schedule.codes),
        )
        return schedule

    def _resolve_all_a(self, start: date, end: date, *, exclude_st: bool) -> UniverseSchedule:
        sql = (
            "SELECT DISTINCT code FROM ohlcv_1d "
            "WHERE type='stock' AND ts >= %s AND ts <= %s"
        )
        frame = self.db.query_dataframe(
            sql, [pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()]
        )
        if frame is None or frame.empty:
            raise DataError(f"all_a 在 {start}..{end} 没有行情")
        codes = {str(code) for code in frame["code"] if str(code).strip()}
        if exclude_st:
            codes -= self._st_codes()
        if not codes:
            raise DataError("all_a 解析后为空（是否全部被判为 ST）")
        if len(codes) > 3000:
            logger.warning(
                "all_a 解析出 %d 只股票，取数与因子计算会很慢；"
                "建议用 Universe.index(...) 或显式池",
                len(codes),
            )
        return UniverseSchedule(codes=tuple(sorted(codes)), label="all_a")

    def _st_codes(self) -> set[str]:
        sql = (
            "SELECT code, last(is_st) AS is_st FROM stock_basic_info GROUP BY code"
        )
        try:
            frame = self.db.query_dataframe(sql, [])
        except Exception as exc:  # noqa: BLE001 - ST 过滤失败不阻断
            logger.warning("ST 名单读取失败，跳过排除: %s", exc)
            return set()
        if frame is None or frame.empty:
            return set()
        return {
            str(row["code"])
            for _, row in frame.iterrows()
            if bool(row.get("is_st")) and pd.notna(row.get("is_st"))
        }

    #: 辅助数据集 -> (原列, 面板列)。回测帧只带 OHLCV，情绪/资金因子靠这些列。
    _EXTRA_COLUMNS: dict[str, dict[str, str]] = {
        "margin_detail": {
            "rzye": "margin_rzye",
            "rzmre": "margin_rzmre",
            "rqye": "margin_rqye",
            "rqyl": "margin_rqyl",
        },
        "moneyflow_daily": {
            "buy_sm_amount": "mf_buy_sm",
            "sell_sm_amount": "mf_sell_sm",
            "buy_lg_amount": "mf_buy_lg",
            "sell_lg_amount": "mf_sell_lg",
            "buy_elg_amount": "mf_buy_elg",
            "sell_elg_amount": "mf_sell_elg",
            "net_mf_amount": "mf_net_amount",
        },
        # 规模因子（size.mv / size.float_mv）需要市值列，OHLCV 帧里没有
        "fin_valuation_daily": {
            "market_cap": "total_mv",
            "float_market_cap": "float_mv",
        },
    }

    def _attach_extras(
        self,
        frame: pd.DataFrame,
        codes: Sequence[str],
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        extras: Sequence[str],
    ) -> pd.DataFrame:
        """按 (symbol, date) 左连接辅助数据；失败只降级，不让回测挂掉。"""
        for dataset in extras:
            mapping = self._EXTRA_COLUMNS.get(str(dataset))
            if not mapping:
                logger.warning("未知辅助数据集 %s，跳过", dataset)
                continue
            placeholders = ",".join(["%s"] * len(codes))
            fields = ", ".join(mapping)
            sql = (
                f"SELECT ts, code, {fields} FROM {dataset} "
                f"WHERE code IN ({placeholders}) AND ts >= %s AND ts <= %s"
            )
            params = [*codes, start_ts.to_pydatetime(), end_ts.to_pydatetime()]
            try:
                aux = self.db.query_dataframe(sql, params)
            except Exception as exc:  # noqa: BLE001 - 辅助数据失败不应阻断回测
                logger.warning("%s 读取失败（相关因子退化为 NaN）: %s", dataset, exc)
                continue
            if aux is None or aux.empty:
                logger.info("%s 在区间内无数据（相关因子退化为 NaN）", dataset)
                continue
            aux = _with_ts(aux)
            aux = aux.rename(columns={"ts": "date", "code": "symbol", **mapping})
            aux["date"] = pd.to_datetime(aux["date"], errors="coerce").dt.normalize()
            aux = aux.dropna(subset=["date", "symbol"])
            for column in mapping.values():
                aux[column] = pd.to_numeric(aux[column], errors="coerce")
            # moneyflow_daily 同一 (ts, code) 有重复行，取最后一条
            aux = aux.drop_duplicates(["symbol", "date"], keep="last")
            frame = frame.merge(aux, on=["symbol", "date"], how="left")
            logger.info("%s 已合入面板: +%d 列", dataset, len(mapping))
        return frame

    def load(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        warmup: int = 0,
        freq: str = "d",
        extras: Sequence[str] = (),
    ) -> DataBundle:
        codes = tuple(sorted(set(str(item) for item in symbols)))
        if not codes:
            raise DataError("QuestDBDataSource 需要至少一个 symbol")
        normalized = normalize_frequency(freq)
        table = ohlc_table_name(normalized)
        start_ts = pd.Timestamp(start).normalize() - pd.Timedelta(days=max(0, int(warmup)) * 2 + 10)
        end_ts = pd.Timestamp(end).normalize()
        placeholders = ",".join(["%s"] * len(codes))
        # 分钟线没有 turnover 列；日线/周线有，且换手类因子（如
        # liquidity.turnover_std_bias_*）必须靠它，否则会退化成 NaN。
        columns = _ohlc_columns(table)
        sql = (
            f"SELECT ts, code AS symbol, {columns} "
            f"FROM {table} "
            f"WHERE code IN ({placeholders}) AND ts >= %s AND ts <= %s "
            "ORDER BY ts, code"
        )
        params = [*codes, start_ts.to_pydatetime(), end_ts.to_pydatetime()]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:
            raise DataError(f"QuestDB 读取失败: {exc}") from exc
        if frame is None or frame.empty:
            raise DataError(f"QuestDB 无数据: {codes} {start}..{end} {normalized}")
        frame = frame.reset_index()
        if "ts" not in frame.columns:
            frame = frame.rename(columns={frame.columns[0]: "ts"})
        frame["date"] = pd.to_datetime(frame["ts"], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["date", "symbol"]).sort_values(["symbol", "date"])
        frame = frame.drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True)
        if "turnover" in frame.columns:
            frame["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce")
        else:
            frame["turnover"] = float("nan")

        factor = pd.to_numeric(frame.get("adj_factor"), errors="coerce")
        fallback = factor.isna()
        frame["adjust_fallback"] = fallback
        frame["adj_factor"] = factor.fillna(1.0)
        frame["hfq_close"] = frame["close"] * frame["adj_factor"]
        anchors = frame.groupby("symbol")["adj_factor"].last().astype(float)
        anchor_map = frame["symbol"].map(anchors).replace(0, 1.0).fillna(1.0)
        frame["adj_close"] = frame["hfq_close"] / anchor_map
        frame = frame[
            [
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "turnover",
                "adj_factor",
                "hfq_close",
                "adj_close",
                "adjust_fallback",
            ]
        ].reset_index(drop=True)

        if extras:
            frame = self._attach_extras(frame, codes, start_ts, end_ts, extras)

        universe = Universe.from_codes(
            codes,
            as_of=pd.Timestamp(start).date(),
            fingerprint=hashlib.sha256("|".join(codes).encode()).hexdigest()[:16],
        )
        return DataBundle(
            frame=frame,
            calendar=_calendar(frame),
            snapshot=make_snapshot(self.name, frame),
            universe=universe,
            security_status=self._load_security_status(codes, end),
        )

    def load_factors(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        factor_ids: Sequence[str],
    ) -> pd.DataFrame:
        """读取因子长表，供 F.factor / factor_source=stored 使用。"""
        codes = tuple(sorted({str(item) for item in symbols}))
        ids = tuple(sorted({str(item) for item in factor_ids}))
        if not codes or not ids:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        code_placeholders = ",".join(["%s"] * len(codes))
        id_placeholders = ",".join(["%s"] * len(ids))
        sql = (
            "SELECT ts, code, factor_id, value FROM \"factor_value_daily\" "
            f"WHERE code IN ({code_placeholders}) "
            f"AND factor_id IN ({id_placeholders}) "
            "AND ts BETWEEN %s AND %s ORDER BY ts, code, factor_id"
        )
        params = [
            *codes,
            *ids,
            pd.Timestamp(start).normalize().to_pydatetime(),
            pd.Timestamp(end).normalize().to_pydatetime(),
        ]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception as exc:
            raise DataError(f"因子读取失败: {exc}") from exc
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["ts", "code", "factor_id", "value"])
        return frame.reset_index()

    def load_benchmark(
        self, code: str, start: date, end: date, *, freq: str = "d"
    ) -> pd.Series | None:
        if normalize_frequency(freq) != "d":
            return None
        sql = (
            "SELECT ts, close FROM ohlcv_1d "
            "WHERE market = 'IDX' AND code = %s AND ts >= %s AND ts <= %s "
            "ORDER BY ts"
        )
        params = [
            str(code),
            pd.Timestamp(start).normalize().to_pydatetime(),
            pd.Timestamp(end).normalize().to_pydatetime(),
        ]
        try:
            frame = self.db.query_dataframe(sql, params)
        except Exception:
            return None
        if frame is None or frame.empty:
            return None
        if "ts" not in frame.columns:
            frame = frame.reset_index()
        frame["date"] = pd.to_datetime(frame["ts"], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
        return pd.Series(
            pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
            index=pd.DatetimeIndex(frame["date"]),
            name=str(code),
        ).dropna()
