from __future__ import annotations

from dataclasses import dataclass

from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, RouteDecision, TaskCandidate, TaskRecord

TRIGGER_SOURCES = {"group_at_me", "p2p"}


@dataclass(frozen=True)
class RoutingResult:
    decision: RouteDecision
    task: TaskRecord | None = None


class CandidateCollector:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def collect(self, message: NormalizedMessage, *, now: str) -> list[TaskCandidate]:
        if not message.chat_id:
            return []
        candidates: dict[int, TaskCandidate] = {}

        if message.reply_to_message_id:
            for task in self.store.get_active_tasks_by_watch_key(
                message.chat_id,
                f"msg:{message.reply_to_message_id}",
                now=now,
            ):
                candidates.setdefault(task.id, TaskCandidate(task, "reply_to_msg"))

        if message.thread_id:
            for task in self.store.get_active_tasks_by_watch_key(
                message.chat_id,
                f"thread:{message.thread_id}",
                now=now,
            ):
                candidates.setdefault(task.id, TaskCandidate(task, "thread"))

        if message.sender_id:
            for task in self.store.get_active_tasks_by_watch_key(
                message.chat_id,
                f"user:{message.sender_id}",
                now=now,
            ):
                candidates.setdefault(task.id, TaskCandidate(task, "sender"))

        if message.direct_mention:
            for task in self.store.get_active_tasks_for_chat(message.chat_id, now=now):
                candidates.setdefault(task.id, TaskCandidate(task, "direct_mention"))

        return list(candidates.values())


class MessageRouter:
    def __init__(self, *, store: SQLiteStore, collector: CandidateCollector | None = None):
        self.store = store
        self.collector = collector or CandidateCollector(store)

    def route(
        self,
        message: NormalizedMessage,
        *,
        source: str,
        inserted: bool,
        now: str,
        watch_until: str,
    ) -> RoutingResult:
        if not inserted:
            return self._audit(message, RouteDecision("ignore", reason="duplicate_message"))

        if message.is_self_message:
            return self._audit(message, RouteDecision("ignore", reason="self_message"))

        if message.sender_role == "owner_message":
            return self._route_owner_message(message, now=now)

        if message.at_all:
            return self._audit(message, RouteDecision("ignore", reason="at_all_suppressed"))

        if source == "group_at_me" and not message.direct_mention:
            return self._audit(message, RouteDecision("ignore", reason="non_direct_mention"))

        candidates = self.collector.collect(message, now=now)
        deterministic = self._deterministic_match(message, candidates, now=now)
        if deterministic is not None:
            self.store.attach_message_to_task(deterministic.task.id, message, watch_until=watch_until)
            return self._audit(
                message,
                RouteDecision(
                    "attach_task",
                    target_task_id=deterministic.task.id,
                    target_task_short_id=deterministic.task.short_id,
                    reason="deterministic_shortcut",
                    candidates_count=len(candidates),
                    shortcut_hit=True,
                    matched_by=deterministic.matched_by,
                ),
                deterministic.task,
            )

        if not candidates and source in TRIGGER_SOURCES and message.chat_id:
            historical = self.store.get_recent_closed_tasks(message.chat_id)
            if historical:
                return self._audit(
                    message,
                    RouteDecision(
                        "ambiguous",
                        reason="closed_recall_router_placeholder",
                        candidates_count=len(historical),
                        router_called=False,
                    ),
                )
            task = self.store.create_task_for_message(message, watch_until=watch_until)
            return self._audit(
                message,
                RouteDecision(
                    "new_task",
                    target_task_id=task.id,
                    target_task_short_id=task.short_id,
                    reason="new_trigger",
                    candidates_count=0,
                    shortcut_hit=False,
                    matched_by="new_trigger",
                ),
                task,
            )

        if source == "active_watch" and not candidates:
            return self._audit(
                message,
                RouteDecision("ignore", reason="active_watch_no_candidate", candidates_count=0),
            )

        if not candidates and source in TRIGGER_SOURCES:
            return self._audit(message, RouteDecision("ignore", reason="missing_chat_id"))

        return self._audit(
            message,
            RouteDecision(
                "ambiguous",
                reason="router_placeholder",
                candidates_count=len(candidates),
                router_called=False,
            ),
        )

    def _route_owner_message(self, message: NormalizedMessage, *, now: str) -> RoutingResult:
        task = self._owner_takeover_task(message, now=now)
        if task is None:
            return self._audit(
                message,
                RouteDecision("ignore", reason="owner_message_not_task_intervention"),
            )
        self.store.close_task_for_owner_takeover(task.id)
        return self._audit(
            message,
            RouteDecision(
                "human_taken_over",
                target_task_id=task.id,
                target_task_short_id=task.short_id,
                reason="owner_message_related_to_active_task",
                candidates_count=1,
                matched_by="owner_takeover",
            ),
            task,
        )

    def _owner_takeover_task(self, message: NormalizedMessage, *, now: str) -> TaskRecord | None:
        if not message.chat_id:
            return None
        if message.reply_to_message_id:
            tasks = [
                self.store.get_task_by_id(task_id)
                for task_id in self.store.find_task_ids_for_message(message.reply_to_message_id)
            ]
            active = [task for task in tasks if _is_active(task, now=now)]
            if len(active) == 1:
                return active[0]
        if message.thread_id:
            active = self.store.get_active_tasks_by_thread(
                message.chat_id,
                message.thread_id,
                now=now,
            )
            if len(active) == 1:
                return active[0]
        if message.chat_type == "p2p":
            active = self.store.get_active_tasks_for_chat(message.chat_id, now=now)
            if len(active) == 1:
                return active[0]
        return None

    def _deterministic_match(
        self,
        message: NormalizedMessage,
        candidates: list[TaskCandidate],
        *,
        now: str,
    ) -> TaskCandidate | None:
        if not message.chat_id:
            return None
        if message.chat_type == "p2p":
            active = self.store.get_active_tasks_for_chat(message.chat_id, now=now)
            if len(active) == 1:
                return TaskCandidate(active[0], "p2p_single_active")
        reply_matches = [candidate for candidate in candidates if candidate.matched_by == "reply_to_msg"]
        if len(reply_matches) == 1:
            return reply_matches[0]
        thread_matches = [candidate for candidate in candidates if candidate.matched_by == "thread"]
        if len(thread_matches) == 1:
            return thread_matches[0]
        return None

    def _audit(
        self,
        message: NormalizedMessage,
        decision: RouteDecision,
        task: TaskRecord | None = None,
    ) -> RoutingResult:
        self.store.record_routing_audit(message_id=message.message_id, decision=decision)
        return RoutingResult(decision=decision, task=task)


def _is_active(task: TaskRecord, *, now: str) -> bool:
    return task.status in {"watching", "waiting_approval"} and (
        task.watch_until is None or task.watch_until > now
    )
