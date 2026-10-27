"""定时任务状态仓储。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from data_interface.common.questdb_client import QuestDBClient
from data_interface.common.timeutil import now_cn

logger = logging.getLogger("ScheduledTasks.State")

_COLUMNS = [
    "updated_at",
    "task_name",
    "dataset",
    "cursor",
    "status",
    "total",
    "processed",
    "message",
    "payload",
]


class TaskStateRepository:
    def __init__(self, db: QuestDBClient | None = None):
        self.db = db or QuestDBClient()

    def get(self, task_name: str, dataset: str) -> dict[str, Any] | None:
        # QuestDB WAL 异步可见；保存后立刻读取可能暂时为空，做短暂重试
        for _ in range(5):
            rows = self.db.query(
                'SELECT * FROM "scheduled_task_state" '
                "WHERE task_name = %s AND dataset = %s "
                "LATEST ON updated_at PARTITION BY task_name, dataset",
                (task_name, dataset),
            )
            if rows:
                return dict(zip(_COLUMNS, rows[0]))
            time.sleep(0.2)
        return None

    def list_latest(self, task_name: str) -> dict[str, dict[str, Any]]:
        """一次取回某任务下所有 dataset 的最新状态。"""
        rows = self.db.query(
            'SELECT * FROM "scheduled_task_state" '
            "WHERE task_name = %s "
            "LATEST ON updated_at PARTITION BY task_name, dataset",
            (task_name,),
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            item = dict(zip(_COLUMNS, row))
            dataset = item.get("dataset")
            if dataset:
                result[str(dataset)] = item
        return result

    def save(
        self,
        task_name: str,
        dataset: str,
        *,
        cursor: str | None,
        status: str,
        total: int,
        processed: int,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = now_cn()
        self.db.execute(
            'INSERT INTO "scheduled_task_state" '
            "(updated_at, task_name, dataset, cursor, status, total, processed, message, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                now,
                task_name,
                dataset,
                cursor,
                status,
                total,
                processed,
                message,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
        # 等 WAL 可见，确保下一次心跳读到最新游标（最多 3s，避免长时间阻塞）
        for _ in range(15):
            rows = self.db.query(
                'SELECT updated_at FROM "scheduled_task_state" '
                "WHERE task_name = %s AND dataset = %s "
                "ORDER BY updated_at DESC LIMIT 1",
                (task_name, dataset),
            )
            if rows and rows[0][0] >= now:
                return
            time.sleep(0.2)
        logger.warning("任务状态写入后等待可见超时: %s/%s", task_name, dataset)
