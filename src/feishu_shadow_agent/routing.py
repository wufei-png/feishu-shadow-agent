from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, RouteDecision, TaskCandidate, TaskRecord

TRIGGER_SOURCES = {"group_at_me", "p2p"}
ROUTER_PLACEHOLDER_REASONS = {"router_placeholder", "closed_recall_router_placeholder"}
TASK_SESSION_ROUTES = {"new_task", "attach_task", "reopen_task"}


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
        retry_incomplete_processing: bool = False,
    ) -> RoutingResult:
        if not inserted and self.store.message_has_routing_audit(message.message_id):
            if retry_incomplete_processing:
                # A duplicate route audit means ingestion was durable, but Hermes
                # processing may have crashed before reaching a terminal marker.
                # Reuse that route only when the downstream stage is still open.
                existing = self.store.get_latest_non_duplicate_routing_decision(message.message_id)
                if existing is not None:
                    decision, task = existing
                    stage = _processing_stage_for_decision(decision)
                    stage_final = stage is not None and self.store.message_processing_is_final(
                        message.message_id,
                        stage=stage,
                    )
                    resource_final = stage == "task_session" and self.store.message_processing_is_final(
                        message.message_id,
                        stage="resource_download",
                    )
                    if stage is not None and not stage_final and not resource_final:
                        if decision.route in TASK_SESSION_ROUTES and task is None:
                            return self._audit(message, RouteDecision("ignore", reason="duplicate_message"))
                        return RoutingResult(decision=decision, task=task)
            return self._audit(message, RouteDecision("ignore", reason="duplicate_message"))

        sent_action_task = self.store.find_task_for_sent_action_message(message.message_id)
        if sent_action_task is not None:
            # Readback messages prove a dispatch happened. Link them to the task
            # audit trail, then ignore them as fresh work to prevent reply loops.
            decision = self.store.record_agent_message_for_task_and_audit(
                sent_action_task,
                message,
                watch_until=watch_until,
            )
            return RoutingResult(decision=decision, task=sent_action_task)

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
            decision = self.store.attach_message_to_task_and_audit(
                deterministic.task,
                message,
                watch_until=watch_until,
                candidates_count=len(candidates),
                matched_by=deterministic.matched_by,
            )
            return RoutingResult(decision=decision, task=deterministic.task)

        if not candidates and source in TRIGGER_SOURCES and message.chat_id:
            historical = self.store.get_related_closed_tasks(
                message,
                since=_minus_days(now, 7),
            )
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
            task, decision = self.store.create_task_for_message_and_audit(
                message,
                watch_until=watch_until,
            )
            return RoutingResult(decision=decision, task=task)

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
        decision = self.store.close_task_for_owner_takeover_and_audit(
            task,
            message,
        )
        return RoutingResult(decision=decision, task=task)

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
            active = self.store.get_active_tasks_by_watch_key(
                message.chat_id,
                f"thread:{message.thread_id}",
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


def _processing_stage_for_decision(decision: RouteDecision) -> str | None:
    if decision.route in TASK_SESSION_ROUTES:
        return "task_session"
    if decision.route == "ambiguous" and decision.reason in ROUTER_PLACEHOLDER_REASONS:
        return "task_router"
    return None


def _minus_days(value: str, days: int) -> str:
    try:
        base = datetime.fromisoformat(value)
    except ValueError:
        base = datetime.now().astimezone()
    return (base - timedelta(days=days)).astimezone().isoformat(timespec="seconds")
