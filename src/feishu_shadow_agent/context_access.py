from __future__ import annotations

from typing import Any

from .config import AppConfig
from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, TaskRecord

CONTEXT_SNAPSHOT_MESSAGES_PER_TASK = 5


class ContextAccessBuilder:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: AppConfig,
        preserve_store_path: bool = False,
    ):
        self.store = store
        self.config = config
        self.preserve_store_path = preserve_store_path

    def router_context_access(
        self,
        *,
        message: NormalizedMessage,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[str, Any] | None:
        context = self.base_context_access()
        if context is None:
            return None
        context["query_scope"] = {
            "current_message_id": message.message_id,
            "active_tasks": [
                context_task_card(candidate.task) for candidate in active_candidates
            ],
            "historical_tasks": [context_task_card(task) for task in historical],
        }
        context["snapshot"] = self.task_context_snapshot(
            [
                *(candidate.task for candidate in active_candidates),
                *historical,
            ]
        )
        return context

    def router_message_counts(
        self,
        *,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[int, int]:
        task_ids = [candidate.task.id for candidate in active_candidates] + [
            task.id for task in historical
        ]
        return self.store.count_task_messages_by_task_ids(task_ids)

    def task_session_context_access(
        self,
        *,
        task: TaskRecord,
    ) -> dict[str, Any] | None:
        context = self.base_context_access()
        if context is None:
            return None
        return {
            "read_only_uri": context["read_only_uri"],
            "allowed_tables": context["allowed_tables"],
            "query_scope": {"task": {"id": task.id}},
        }

    def base_context_access(self) -> dict[str, Any] | None:
        path = self.store.path.expanduser()
        if not path.exists():
            return None
        uri_path = path.absolute() if self.preserve_store_path else path.resolve()
        return {
            "backend": "sqlite",
            "mode": "live_read_only",
            "read_only_uri": f"{uri_path.as_uri()}?mode=ro",
            "allowed_tables": [
                "tasks",
                "task_messages",
                "messages",
                "resources",
                "routing_audits",
            ],
        }

    def task_context_snapshot(self, tasks: list[TaskRecord]) -> dict[str, Any]:
        task_ids = [task.id for task in tasks]
        by_task_id = self.store.list_recent_task_context(
            task_ids,
            messages_per_task=CONTEXT_SNAPSHOT_MESSAGES_PER_TASK,
        )
        return {
            "type": "bounded_recent_task_messages",
            "message_limit_per_task": CONTEXT_SNAPSHOT_MESSAGES_PER_TASK,
            "tasks": [
                context_task_card(task) | by_task_id.get(task.id, {}) for task in tasks
            ],
        }


def context_task_card(task: TaskRecord) -> dict[str, Any]:
    return {"id": task.id, "short_id": task.short_id}
