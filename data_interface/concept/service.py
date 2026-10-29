"""概念板块服务：同步编排、PIT 查询。"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Sequence

import pandas as pd

from data_interface.common.config import get_config
from data_interface.common.timeutil import now_cn

from .models import BOARD_TYPE_CONCEPT, SOURCE_THS, Board
from .repository import ConceptRepository
from .sources.em_slist_source import EmSlistBoardSource, classify_board
from .sources.em_source import EmConceptSource
from .sources.ths_scrape_source import ThsScrapeConceptSource
from .sources.ths_source import ThsConceptSource
from .sources.tushare_classification import TushareConceptClassificationSource

logger = logging.getLogger("DataInterface.Concept.Service")


class ConceptService:
    def __init__(
        self,
        *,
        repository: ConceptRepository | None = None,
        board_sources: Sequence[Any] | None = None,
        daily_source: Any | None = None,
        member_sources: Sequence[Any] | None = None,
        classification_source: Any | None = None,
        board_source: EmSlistBoardSource | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("concept", {}) or {}
        self.board_settings = self.config.get("boards", {}) or {}
        self.repository = repository or ConceptRepository(config=self.config)
        self.board_sources = list(
            board_sources
            if board_sources is not None
            else [ThsConceptSource(), EmConceptSource()]
        )
        self.daily_source = daily_source or ThsConceptSource()
        self.member_sources = list(
            member_sources
            if member_sources is not None
            else [EmConceptSource(), ThsScrapeConceptSource()]
        )
        self.classification_source = (
            classification_source or TushareConceptClassificationSource(self.config)
        )
        self.board_source = board_source or EmSlistBoardSource(
            request_interval=float(self.board_settings.get("request_interval_seconds", 0.12)),
            retries=int(self.board_settings.get("max_retries", 4)),
        )
        self.max_member_boards = int(
            self.settings.get("member_max_boards_per_run", 20)
        )
        self.member_drop_guard = float(
            self.settings.get("member_drop_guard", 0.7)
        )

    # ------------------------------------------------------------------ 板块
    def sync_boards(self, as_of: str | date | None = None) -> dict[str, Any]:
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        now = now_cn()
        errors: list[dict[str, str]] = []
        for source in self.board_sources:
            try:
                frame = source.list_boards()
            except Exception as exc:
                errors.append({"source": source.source_id, "error": str(exc)})
                logger.warning("板块列表源失败 %s: %s", source.source_id, exc)
                continue
            if frame is None or frame.empty:
                continue
            frame = frame.copy()
            frame["ts"] = snapshot
            frame["updated_at"] = now
            if "status" not in frame.columns:
                frame["status"] = "active"
            if "board_type" not in frame.columns:
                frame["board_type"] = BOARD_TYPE_CONCEPT
            if "source" not in frame.columns:
                frame["source"] = source.source_id
            if "source_code" not in frame.columns:
                frame["source_code"] = frame["board_code"]
            rows = self.repository.upsert_boards(frame)
            self._wait_for_boards()
            return {
                "dataset": "concept_meta",
                "done": True,
                "source": source.source_id,
                "rows_written": rows,
                "as_of": str(snapshot.date()),
                "errors": errors,
            }
        return {
            "dataset": "concept_meta",
            "done": False,
            "source": None,
            "rows_written": 0,
            "as_of": str(snapshot.date()),
            "errors": errors or [{"error": "all board sources empty"}],
        }

    def list_boards(self, as_of: str | date | None = None) -> pd.DataFrame:
        return self.repository.list_boards(as_of=as_of)

    def boards(self, as_of: str | date | None = None) -> list[Board]:
        return self._board_objects(as_of=as_of)

    def _board_objects(
        self,
        codes: Sequence[str] | None = None,
        *,
        as_of: str | date | None = None,
    ) -> list[Board]:
        frame = self.repository.list_boards(as_of=as_of)
        if frame.empty:
            return []
        wanted = {str(code) for code in codes} if codes else None
        result: list[Board] = []
        for row in frame.to_dict("records"):
            code = str(row.get("board_code"))
            if wanted is not None and code not in wanted:
                continue
            result.append(
                Board(
                    board_code=code,
                    name=str(row.get("name")),
                    source=str(row.get("source") or SOURCE_THS),
                    source_code=str(row.get("source_code") or code),
                    board_type=str(row.get("board_type") or BOARD_TYPE_CONCEPT),
                )
            )
        return sorted(result, key=lambda item: item.board_code)

    # ------------------------------------------------------------------ 日线
    def sync_daily(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        *,
        board_codes: Sequence[str] | None = None,
        max_boards: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not self._has_boards():
            self.sync_boards()
        boards = self._board_objects(board_codes)
        if not boards:
            return {
                "dataset": "concept_daily",
                "done": True,
                "rows_written": 0,
                "next_cursor": None,
                "errors": [{"error": "no boards in concept_board_dim"}],
            }

        start = start_date or (now_cn().date() - timedelta(days=30))
        end = end_date or now_cn().date()
        remaining = [item for item in boards if not cursor or item.board_code >= cursor]
        batch = remaining[: max(1, int(max_boards))]
        rows_written = 0
        errors: list[dict[str, str]] = []

        for board in batch:
            try:
                written = self.sync_one_board_daily(board, start, end)
            except Exception as exc:
                errors.append({"board_code": board.board_code, "error": str(exc)})
                logger.warning("板块日线失败 %s: %s", board.board_code, exc)
                continue
            if written == 0:
                errors.append({"board_code": board.board_code, "error": "empty"})
                continue
            rows_written += written

        tail = remaining[len(batch) :]
        return {
            "dataset": "concept_daily",
            "done": not tail,
            "rows_written": rows_written,
            "next_cursor": tail[0].board_code if tail else None,
            "processed": [item.board_code for item in batch],
            "errors": errors,
        }

    def sync_one_board_daily(
        self,
        board: Board,
        start_date: str | date,
        end_date: str | date,
        *,
        fetched_at: Any | None = None,
    ) -> int:
        """同步单个板块的日线，返回写入行数（供批量同步与回补脚本复用）。"""
        frame = self.daily_source.fetch_board_daily(board, start_date, end_date)
        if frame is None or frame.empty:
            return 0
        frame = frame.copy()
        frame["board_code"] = board.board_code
        frame["board_name"] = board.name
        frame["source"] = self.daily_source.source_id
        frame["quality"] = "ok"
        frame["fetched_at"] = fetched_at or now_cn()
        return self.repository.upsert_daily(frame)

    def daily(
        self,
        board_codes: Sequence[str],
        start_date: str | date,
        end_date: str | date,
        *,
        source: str | None = None,
    ) -> pd.DataFrame:
        frame = self.repository.daily(board_codes, start_date, end_date, source=source)
        # QuestDB WAL 异步可见：刚同步完立刻查询可能为空，短暂重试一次
        if frame is None or frame.empty:
            time.sleep(0.3)
            frame = self.repository.daily(board_codes, start_date, end_date, source=source)
        return frame

    # ------------------------------------------------------------------ 成分
    def sync_members(
        self,
        *,
        board_codes: Sequence[str] | None = None,
        max_boards: int | None = None,
        cursor: str | None = None,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        if not self._has_boards():
            self.sync_boards()
        boards = self._board_objects(board_codes)
        if not boards:
            return {
                "dataset": "concept_member",
                "done": True,
                "rows_written": 0,
                "next_cursor": None,
                "errors": [{"error": "no boards in concept_board_dim"}],
            }

        limit = int(max_boards or self.max_member_boards)
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        remaining = [item for item in boards if not cursor or item.board_code >= cursor]
        batch = remaining[: max(1, limit)]
        now = now_cn()
        rows_written = 0
        errors: list[dict[str, str]] = []
        sources_used: dict[str, int] = {}

        for board in batch:
            previous = self.repository.member_count(board.board_code)
            frame = None
            used_source = ""
            partial = False
            for source in self.member_sources:
                try:
                    candidate = source.fetch_members(board)
                except Exception as exc:
                    logger.warning(
                        "成分源 %s 拉取 %s 失败: %s", source.source_id, board.board_code, exc
                    )
                    continue
                if candidate is not None and not candidate.empty:
                    frame = candidate
                    used_source = source.source_id
                    partial = bool(candidate.attrs.get("partial"))
                    break
            if frame is None or frame.empty:
                errors.append({"board_code": board.board_code, "error": "empty"})
                continue
            if partial and previous and len(frame) < previous * self.member_drop_guard:
                errors.append(
                    {
                        "board_code": board.board_code,
                        "error": (
                            f"partial snapshot dropped from {previous} to {len(frame)}，"
                            "已跳过以避免覆盖完整快照"
                        ),
                    }
                )
                continue

            frame = frame.copy()
            frame["ts"] = snapshot
            frame["board_code"] = board.board_code
            frame["quality"] = "partial" if partial else "ok"
            frame["source"] = used_source
            frame["fetched_at"] = now
            if "weight" not in frame.columns:
                frame["weight"] = None
            if "in_date" not in frame.columns:
                frame["in_date"] = None
            rows_written += self.repository.upsert_members(frame)
            sources_used[used_source] = sources_used.get(used_source, 0) + len(frame)

        tail = remaining[len(batch) :]
        return {
            "dataset": "concept_member",
            "done": not tail,
            "rows_written": rows_written,
            "next_cursor": tail[0].board_code if tail else None,
            "as_of": str(snapshot.date()),
            "sources": sources_used,
            "errors": errors,
        }

    def members(
        self, board: str, as_of: str | date | None = None
    ) -> pd.DataFrame:
        matched = self.repository.find_board(board, as_of=as_of)
        if matched is None:
            return pd.DataFrame()
        frame = self.repository.members(str(matched["board_code"]), as_of=as_of)
        if frame is None or frame.empty:
            # WAL 可见性重试；真正的空快照仍然返回空
            time.sleep(0.3)
            frame = self.repository.members(str(matched["board_code"]), as_of=as_of)
        if frame is not None and not frame.empty:
            frame.attrs["board_code"] = str(matched["board_code"])
            frame.attrs["board_name"] = str(matched["name"])
        return frame

    # ------------------------------------------------ 个股↔板块（东财 slist）
    def sync_stock_boards(
        self,
        codes: Sequence[str],
        *,
        as_of: str | date | None = None,
    ) -> dict[str, Any]:
        """按股票批量刷新「个股 → 所属板块」映射，并顺带补 ``dim_board``。

        东财 ``slist`` 一次请求即可拿到个股全部板块归属，因此**以股票为主循环**，
        比「按板块拉成分」快 3 个数量级；``is_primary`` 取返回列表第 1 项
        （东财把「所属行业」排在首位，属启发式，已在源码中标注）。
        """
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        now = now_cn()
        map_frames: list[pd.DataFrame] = []
        dim_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        errors: list[dict[str, str]] = []

        for code in codes:
            try:
                frame = self.board_source.fetch_stock_boards(code)
            except Exception as exc:  # noqa: BLE001
                errors.append({"code": str(code), "error": str(exc)})
                continue
            if frame is None or frame.empty:
                errors.append({"code": str(code), "error": "empty"})
                continue
            frame = frame.copy()
            frame["ts"] = snapshot
            frame["is_primary"] = frame["rank"] == 0
            frame["effective_from"] = snapshot
            frame["effective_to"] = None
            frame["source"] = self.board_source.source_id
            frame["code"] = frame["code"].astype(str)
            map_frames.append(frame)
            for row in frame.to_dict("records"):
                key = (str(row["board_id"]), str(row["board_type"]), self.board_source.source_id)
                dim_rows.setdefault(
                    key,
                    {
                        "ts": snapshot,
                        "board_id": str(row["board_id"]),
                        "board_type": str(row["board_type"]),
                        "board_name": str(row["board_name"]),
                        "parent_id": None,
                        "source": self.board_source.source_id,
                    },
                )

        map_written = 0
        if map_frames:
            merged = pd.concat(map_frames, ignore_index=True)
            map_written = self.repository.upsert_map_stock_board(merged)
        dim_written = 0
        if dim_rows:
            dim_frame = pd.DataFrame(list(dim_rows.values()))
            dim_frame["updated_at"] = now
            dim_written = self.repository.upsert_dim_board(dim_frame)

        return {
            "dataset": "map_stock_board",
            "done": True,
            "as_of": str(snapshot.date()),
            "codes": len(codes),
            "map_rows": map_written,
            "dim_rows": dim_written,
            "errors": errors,
        }

    def stock_boards(
        self, code: str, as_of: str | date | None = None
    ) -> pd.DataFrame:
        return self.repository.stock_boards(code, as_of=as_of)

    def board_members(self, board_id: str, as_of: str | date | None = None) -> list[str]:
        return self.repository.board_members(board_id, as_of=as_of)

    # ------------------------------------------------ 自算板块日线（成分加权）
    def compute_board_daily(
        self,
        board_ids: Sequence[str],
        start_date: str | date,
        end_date: str | date,
        *,
        weight: str = "equal",
        as_of: str | date | None = None,
        write: bool = True,
    ) -> dict[str, Any]:
        """用「成分股 + 流通市值」自算板块指数，替代外部板块行情。

        为什么自算：外部板块行情存在两套编码（同花顺 3000xx / 东财 BKxxxx）、
        当日 ``pct_chg`` 常为 NULL，且与个股对齐性差；用成分股自算后
        「板块收益 / 个股超额」天然可比。

        收益口径：``board_ret_t = Σ w_i,t-1 · r_i,t / Σ w_i,t-1``，
        ``w`` 默认取前一日流通市值（缺失时退化为等权），指数点位以 1000 为基点。
        """
        ids = [str(item) for item in board_ids if str(item)]
        if not ids:
            return {"dataset": "board_daily", "rows": 0, "boards": 0, "errors": [{"error": "no board_ids"}]}
        snapshot = pd.Timestamp(as_of or now_cn().date()).normalize()
        start = pd.Timestamp(start_date)
        load_start = (start - pd.Timedelta(days=10)).strftime("%Y-%m-%d")

        members = {board_id: self.repository.board_members(board_id, as_of=snapshot) for board_id in ids}
        all_codes = sorted({code for codes in members.values() for code in codes})
        if not all_codes:
            return {"dataset": "board_daily", "rows": 0, "boards": 0, "errors": [{"error": "no members"}]}

        prices = self.repository.daily_prices(all_codes, load_start, end_date)
        if prices.empty:
            return {"dataset": "board_daily", "rows": 0, "boards": 0, "errors": [{"error": "no prices"}]}
        prices["ts"] = pd.to_datetime(prices["ts"]).dt.tz_localize(None) if prices["ts"].dt.tz is not None else pd.to_datetime(prices["ts"])
        price_wide = prices.pivot_table(index="ts", columns="code", values="close", aggfunc="last").sort_index()
        returns = price_wide.pct_change()

        caps = None
        if weight == "float_mv":
            caps = self.repository.float_market_caps(all_codes, load_start, end_date)
            if not caps.empty:
                caps["ts"] = pd.to_datetime(caps["ts"]).dt.tz_localize(None) if caps["ts"].dt.tz is not None else pd.to_datetime(caps["ts"])
                caps = caps.pivot_table(
                    index="ts", columns="code", values="float_market_cap", aggfunc="last"
                ).sort_index()
            else:
                caps = None

        names = {
            str(row["board_id"]): str(row["board_name"])
            for row in self.repository.dim_boards().to_dict("records")
        }

        records: list[dict[str, Any]] = []
        for board_id in ids:
            codes = [code for code in members[board_id] if code in returns.columns]
            if not codes:
                continue
            rets = returns[codes]
            if caps is not None:
                caps_prev = caps[codes].reindex(rets.index).shift(1)
                weights = caps_prev.div(caps_prev.sum(axis=1), axis=0)
                weights = weights.where(caps_prev.notna(), 0.0)
                weighted = (rets * weights).sum(axis=1) / weights.sum(axis=1).replace(0, pd.NA)
            else:
                weighted = rets.mean(axis=1)
            series = weighted.dropna()
            if series.empty:
                continue
            window = series[series.index >= start]
            if window.empty:
                continue
            level = 1000.0 * (1 + series).cumprod()
            level = level[level.index >= start]
            for ts, value in level.items():
                records.append(
                    {
                        "ts": ts,
                        "board_code": board_id,
                        "board_name": names.get(board_id, ""),
                        "open": float(value / (1 + series.loc[ts])) if ts in series.index else float(value),
                        "high": float(value),
                        "low": float(value),
                        "close": round(float(value), 4),
                        "volume": 0,
                        "amount": 0.0,
                        "pct_chg": round(float(series.loc[ts]) * 100, 4),
                        "quality": "computed",
                        "source": "computed.bk",
                        "fetched_at": now_cn(),
                    }
                )

        written = 0
        if write and records:
            written = self.repository.upsert_daily(pd.DataFrame(records))
        return {
            "dataset": "board_daily",
            "boards": len(ids),
            "members": len(all_codes),
            "rows": written or len(records),
            "written": written,
        }

    # ------------------------------------------------------------------ 分类
    def sync_classification(self) -> dict[str, Any]:
        try:
            frame = self.classification_source.fetch_classification()
        except Exception as exc:
            return {
                "dataset": "concept_class",
                "done": False,
                "rows_written": 0,
                "errors": [{"error": str(exc)}],
            }
        if frame is None or frame.empty:
            return {
                "dataset": "concept_class",
                "done": False,
                "rows_written": 0,
                "errors": [{"error": "empty classification"}],
            }
        frame = frame.copy()
        frame["source"] = self.classification_source.source_id
        frame["fetched_at"] = now_cn()
        rows = self.repository.upsert_classification(frame)
        return {
            "dataset": "concept_class",
            "done": True,
            "rows_written": rows,
            "as_of": str(pd.Timestamp(frame["ts"].iloc[0]).date()),
            "errors": [],
        }

    def classification(self, as_of: str | date | None = None) -> pd.DataFrame:
        return self.repository.classification(as_of=as_of)

    # ------------------------------------------------------------------ 内部
    def _wait_for_boards(self, timeout: float = 3.0) -> pd.DataFrame:
        """QuestDB WAL 异步可见：写完立刻读可能为空，短暂等待。"""
        deadline = time.monotonic() + timeout
        frame = self.repository.list_boards()
        while frame.empty and time.monotonic() < deadline:
            time.sleep(0.2)
            frame = self.repository.list_boards()
        return frame

    def _has_boards(self) -> bool:
        return not self._wait_for_boards(timeout=1.0).empty

    def board_codes(self) -> list[str]:
        return [item.board_code for item in self._board_objects()]
