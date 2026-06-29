from __future__ import annotations

from typing import Any

from .config import AppConfig
from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, TaskRecord


class ContextAccessBuilder:
    def __init__(self, *, store: SQLiteStore, config: AppConfig):
        self.store = store
        self.config = config

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
            "active_tasks": [context_task_card(candidate.task) for candidate in active_candidates],
            "historical_tasks": [context_task_card(task) for task in historical],
        }
        return context

    def router_message_counts(
        self,
        *,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[int, int]:
        task_ids = [candidate.task.id for candidate in active_candidates] + [task.id for task in historical]
        return self.store.count_task_messages_by_task_ids(task_ids)

    def task_session_context_access(
        self,
        *,
        message: NormalizedMessage,
        task: TaskRecord,
    ) -> dict[str, Any] | None:
        context = self.base_context_access()
        if context is None:
            return None
        context["query_scope"] = {
            "current_message_id": message.message_id,
            "task": context_task_card(task),
        }
        return context

    def base_context_access(self) -> dict[str, Any] | None:
        if self.config.tool_permissions not in {"guarded_write", "full_access"}:
            return None
        path = self.store.path.expanduser()
        if not path.exists():
            return None
        return {
            "backend": "sqlite",
            "mode": "live_read_only",
            "read_only_uri": f"{path.resolve().as_uri()}?mode=ro",
            "allowed_tables": ["tasks", "task_messages", "messages", "resources", "routing_audits"],
        }


def context_task_card(task: TaskRecord) -> dict[str, Any]:
    return {"id": task.id, "short_id": task.short_id}
