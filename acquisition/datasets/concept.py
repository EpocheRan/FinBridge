"""概念板块同步。"""

from __future__ import annotations

from .base import *  # noqa: F401,F403


class ConceptSyncMixin:
    def _get_concept_service(self) -> ConceptService:
        if self._concept_service is None:
            self._concept_service = ConceptService(
                config=self.config,
                repository=ConceptRepository(db=self.db, config=self.config),
                board_sources=[ThsConceptSource(), EmConceptSource()],
                daily_source=ThsConceptSource(),
                member_sources=[EmConceptSource(), ThsScrapeConceptSource()],
                classification_source=TushareConceptClassificationSource(
                    self.config, tushare=self.tushare
                ),
            )
        return self._concept_service

    def sync_concept_meta(self) -> dict[str, Any]:
        result = self._get_concept_service().sync_boards()
        self.state.save(
            TASK_NAME,
            CONCEPT_META_STATE,
            cursor=None,
            status="DONE" if result.get("done") else "ERROR",
            total=int(result.get("rows_written") or 0),
            processed=int(result.get("rows_written") or 0),
            message=result.get("source"),
            payload={"as_of": result.get("as_of")},
        )
        return result

    def sync_concept_daily(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        board_codes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        start = str(start_date or (now_cn().date() - timedelta(days=30)))[:10]
        end = str(end_date or today_str())[:10]
        previous = None if reset else self.state.get(TASK_NAME, CONCEPT_DAILY_STATE)
        current_cursor = cursor or (previous or {}).get("cursor")
        result = self._get_concept_service().sync_daily(
            start,
            end,
            max_boards=max_boards or 50,
            cursor=current_cursor,
            board_codes=board_codes,
        )
        self.state.save(
            TASK_NAME,
            CONCEPT_DAILY_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("rows_written") or 0),
            processed=len(result.get("processed") or []),
            message=f"rows={result.get('rows_written', 0)}",
            payload={"start_date": start, "end_date": end},
        )
        result.update({"start_date": start, "end_date": end})
        return result

    def sync_concept_member(
        self,
        *,
        max_boards: int | None = None,
        cursor: str | None = None,
        reset: bool = False,
        as_of: str | None = None,
        board_codes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        previous = None if reset else self.state.get(TASK_NAME, CONCEPT_MEMBER_STATE)
        current_cursor = cursor or (previous or {}).get("cursor")
        result = self._get_concept_service().sync_members(
            max_boards=max_boards,
            cursor=current_cursor,
            as_of=as_of,
            board_codes=board_codes,
        )
        self.state.save(
            TASK_NAME,
            CONCEPT_MEMBER_STATE,
            cursor=result.get("next_cursor"),
            status="DONE" if result.get("done") else "RUNNING",
            total=int(result.get("rows_written") or 0),
            processed=len(result.get("sources") or {}),
            message=f"rows={result.get('rows_written', 0)}",
            payload={"as_of": result.get("as_of")},
        )
        return result

    def sync_concept_classification(self) -> dict[str, Any]:
        result = self._get_concept_service().sync_classification()
        self.state.save(
            TASK_NAME,
            CONCEPT_CLASS_STATE,
            cursor=None,
            status="DONE" if result.get("done") else "ERROR",
            total=int(result.get("rows_written") or 0),
            processed=int(result.get("rows_written") or 0),
            message=None if result.get("done") else str(result.get("errors")),
            payload={"as_of": result.get("as_of")},
        )
        return result

    # ------------------------------------------------------------------ 编排
