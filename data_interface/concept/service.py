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
        config: dict[str, Any] | None = None,
    ):
        self.config = config or get_config()
        self.settings = self.config.get("concept", {}) or {}
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
