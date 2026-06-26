from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Callable

from pydantic import ValidationError

from .agent_backend import AgentBackend, AgentRunResult
from .config import AppConfig, ChatPolicyConfig
from .jsonl import JSONLLogger
from .prompt import (
    BaseTaskSessionOutput,
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    TaskRouterOutput,
    build_router_prompt,
    build_task_session_prompt,
)
from .routing import CandidateCollector, RoutingResult
from .store.sqlite_store import SQLiteStore
from .types import NormalizedMessage, RouteDecision, TaskRecord

AGENT_MAX_ATTEMPTS = 3
RESOURCE_MAX_ATTEMPTS = 3
AGENT_AT_SPAN_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
FORBIDDEN_MENTION_RE = re.compile(r"<at\b[^>]*>|</at>|@所有人|@_all|@all", re.IGNORECASE)
TERMINAL_AGENT_ERROR_MARKERS = (
    "no such file",
    "not found",
    "permission denied",
    "unknown option",
    "invalid option",
    "missing required",
    "configuration",
    "config",
    "auth",
    "permission",
)


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    task_id: int | None = None
    action_id: int | None = None
    approval_id: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class ComposedReply:
    text: str
    had_forbidden_mentions: bool


@dataclass(frozen=True)
class AgentAttemptOutcome:
    result: AgentRunResult | None
    attempt_count: int
    last_error: str | None = None


@dataclass(frozen=True)
class ResourcePreflightResult:
    allow: bool
    reason: str
    resources: list[Any]
    attempt_count: int = 0
    last_error: str | None = None


class SendComposer:
    """Build Feishu-safe reply text after the agent proposes plain content."""

    def __init__(self, *, owner_open_id: str):
        self.owner_open_id = owner_open_id

    def compose(
        self,
        *,
        proposed_reply: str,
        reply_target: Any,
        chat_type: str | None,
    ) -> ComposedReply:
        had_forbidden = bool(AGENT_AT_SPAN_RE.search(proposed_reply) or FORBIDDEN_MENTION_RE.search(proposed_reply))
        cleaned = AGENT_AT_SPAN_RE.sub("", proposed_reply)
        cleaned = FORBIDDEN_MENTION_RE.sub("", cleaned)
        cleaned = " ".join(cleaned.split())
        if chat_type == "group" and reply_target is not None:
            sender_id = reply_target["sender_id"]
            sender_role = reply_target["sender_role"]
            if sender_id and sender_id != self.owner_open_id and sender_role not in {"bot_message", "agent_message"}:
                display = _escape_mention_display(reply_target["sender_name"] or sender_id)
                cleaned = f'<at user_id="{sender_id}">{display}</at> {cleaned}'.strip()
        return ComposedReply(text=cleaned, had_forbidden_mentions=had_forbidden)


class ApprovalService:
    def __init__(self, *, store: SQLiteStore, config: AppConfig):
        self.store = store
        self.config = config

    def request_send_reply(
        self,
        *,
        task: TaskRecord,
        reply_target_message_id: str,
        proposed_reply: str,
        reason: str,
        final_reply: str | None = None,
        approvable: bool = True,
    ) -> int:
        reply_text = proposed_reply if final_reply is None else final_reply
        payload_text = reply_text if approvable else ""
        commands = [
            f"/send {task.short_id} <final reply>",
            f"/reject {task.short_id}",
        ]
        if approvable:
            commands.insert(0, f"/approve {task.short_id}")
        payload = {
            "reply_target_message_id": reply_target_message_id,
            "text": payload_text,
            "identity": "user",
            "source": "approval_request",
            "reason": reason,
            "approvable": approvable,
        }
        notify = {
            "type": "approval_required",
            "task_id": task.short_id,
            "reason": reason,
            "preview": proposed_reply,
            "commands": commands,
        }
        return self.store.create_send_reply_approval(
            task_id=task.id,
            preview=proposed_reply,
            payload=payload,
            notify_payload=notify,
        )

    def notify_owner(self, *, task: TaskRecord | None, reason: str, payload: dict[str, Any] | None = None) -> int:
        data = {"type": "owner_notification", "reason": reason} | (payload or {})
        if task is not None:
            data["task_id"] = task.short_id
        return self.store.create_owner_notification_action(task_id=None if task is None else task.id, payload=data)

    def apply_command(self, *, message: NormalizedMessage) -> dict[str, Any] | None:
        command = message.text.strip()
        if not command.startswith("/"):
            return None
        match = re.match(r"^/(\S+)(?:\s+(\S+))?(?:\s+([\s\S]*))?$", command)
        if match is None:
            return None
        verb = match.group(1)
        target_id = match.group(2)
        final_reply = match.group(3)
        if verb in {"approve", "reject"} and target_id and final_reply is None:
            return self.store.apply_approval_command(
                message_id=message.message_id,
                command=command,
                verb=verb,
                target_id=target_id,
            )
        if verb == "send" and target_id and final_reply is not None:
            return self.store.apply_approval_command(
                message_id=message.message_id,
                command=command,
                verb=verb,
                target_id=target_id,
                final_reply=final_reply,
            )
        return self.store.apply_approval_command(
            message_id=message.message_id,
            command=command,
            verb="invalid",
            target_id="",
        )


class TaskProcessingService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        config: AppConfig,
        agent_backend: AgentBackend,
        logger: JSONLLogger,
        agent_max_attempts: int = AGENT_MAX_ATTEMPTS,
        agent_retry_delays_seconds: tuple[float, ...] = (1.0, 3.0),
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.config = config
        self.agent_backend = agent_backend
        self.logger = logger
        self.collector = CandidateCollector(store)
        self.approvals = ApprovalService(store=store, config=config)
        self.composer = SendComposer(owner_open_id=config.owner.open_id)
        self.agent_max_attempts = max(1, agent_max_attempts)
        self.agent_retry_delays_seconds = agent_retry_delays_seconds
        self.sleep_func = sleep_func
        self.resource_retry_func: Callable[[NormalizedMessage, str | None], None] | None = None

    def set_resource_retry_func(self, func: Callable[[NormalizedMessage, str | None], None]) -> None:
        self.resource_retry_func = func

    def process(
        self,
        *,
        message: NormalizedMessage,
        routing: RoutingResult,
        source: str,
        now: str,
        watch_until: str,
        run_id: str,
    ) -> ProcessingResult | None:
        route = routing.decision.route
        reason = routing.decision.reason
        self.logger.debug(
            "task_processing_started",
            run_id=run_id,
            task_id=None if routing.task is None else str(routing.task.id),
            data={
                "message_id": message.message_id,
                "source": source,
                "route": route,
                "reason": reason,
            },
        )
        if route in {"ignore", "human_taken_over", "close_task"}:
            self.logger.debug(
                "task_processing_skipped",
                run_id=run_id,
                task_id=None if routing.task is None else str(routing.task.id),
                data={
                    "message_id": message.message_id,
                    "source": source,
                    "route": route,
                    "reason": reason,
                },
            )
            return None
        if route == "ambiguous" and reason in {"router_placeholder", "closed_recall_router_placeholder"}:
            routed = self._run_task_router(
                message=message,
                source=source,
                reason=reason,
                now=now,
                watch_until=watch_until,
                run_id=run_id,
            )
            if isinstance(routed, ProcessingResult):
                return routed
            if routed is None or routed.task is None:
                return None
            routing = routed
        if routing.decision.route in {"new_task", "attach_task", "reopen_task"} and routing.task is not None:
            return self._run_task_session(
                task=routing.task,
                message=message,
                now=now,
                watch_until=watch_until,
                run_id=run_id,
            )
        return None

    def _call_agent_with_retries(
        self,
        call: Callable[[], AgentRunResult],
        *,
        run_id: str | None,
        stage: str,
        message_id: str,
        task_id: int | None = None,
    ) -> AgentAttemptOutcome:
        last_result: AgentRunResult | None = None
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self.agent_max_attempts + 1):
            attempts = attempt
            self.logger.debug(
                "agent_call_attempt_started",
                run_id=run_id,
                task_id=None if task_id is None else str(task_id),
                data={"stage": stage, "message_id": message_id, "attempt": attempt},
            )
            try:
                result = call()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.agent_max_attempts:
                    self.logger.warning(
                        "agent_call_retrying",
                        run_id=run_id,
                        task_id=None if task_id is None else str(task_id),
                        data={
                            "stage": stage,
                            "message_id": message_id,
                            "attempt": attempt,
                            "error": last_error,
                        },
                    )
                    self._sleep_before_retry(attempt)
                else:
                    self.logger.error(
                        "agent_call_exception",
                        run_id=run_id,
                        task_id=None if task_id is None else str(task_id),
                        data={
                            "stage": stage,
                            "message_id": message_id,
                            "attempt": attempt,
                            "error": last_error,
                        },
                    )
                continue
            last_result = result
            if result.ok:
                self.logger.debug(
                    "agent_call_succeeded",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "latency_ms": result.latency_ms,
                    },
                )
                return AgentAttemptOutcome(result=result, attempt_count=attempt)
            last_error = _agent_result_error(result)
            if not _is_retryable_agent_result(result):
                self.logger.error(
                    "agent_call_failed_terminal",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                return AgentAttemptOutcome(result=result, attempt_count=attempt, last_error=last_error)
            if attempt < self.agent_max_attempts:
                self.logger.warning(
                    "agent_call_retrying",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                self._sleep_before_retry(attempt)
            else:
                self.logger.error(
                    "agent_call_failed",
                    run_id=run_id,
                    task_id=None if task_id is None else str(task_id),
                    data={
                        "stage": stage,
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": last_error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
        return AgentAttemptOutcome(result=last_result, attempt_count=attempts, last_error=last_error)

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt <= 0 or not self.agent_retry_delays_seconds:
            return
        index = min(attempt - 1, len(self.agent_retry_delays_seconds) - 1)
        delay = self.agent_retry_delays_seconds[index]
        if delay > 0:
            self.sleep_func(delay)

    def _mark_processing_processed(
        self,
        *,
        message: NormalizedMessage,
        stage: str,
        task_id: int | None,
        attempt_count: int,
    ) -> None:
        self.store.record_message_processing(
            message_id=message.message_id,
            task_id=task_id,
            stage=stage,
            status="processed",
            attempt_count=attempt_count,
        )

    def _mark_processing_terminal(
        self,
        *,
        message: NormalizedMessage,
        stage: str,
        task_id: int | None,
        attempt_count: int,
        last_error: str | None,
        terminal_reason: str,
    ) -> None:
        self.store.record_message_processing(
            message_id=message.message_id,
            task_id=task_id,
            stage=stage,
            status="processing_failed_terminal",
            attempt_count=attempt_count,
            last_error=_truncate_error(last_error),
            terminal_reason=terminal_reason,
        )

    def _notify_processing_failed(
        self,
        *,
        message: NormalizedMessage,
        task: TaskRecord | None,
        stage: str,
        attempt_count: int,
        last_error: str | None,
        reason: str,
    ) -> int:
        return self.approvals.notify_owner(
            task=task,
            reason=reason,
            payload={
                "type": "processing_failed",
                "message_id": message.message_id,
                "stage": stage,
                "attempt_count": attempt_count,
                "error": _truncate_error(last_error),
                "message": "Agent processing failed; no reply was generated.",
                "dedupe_key": f"owner-processing-failed:{message.message_id}:{stage}",
            },
        )

    def _notify_resource_blocked(
        self,
        *,
        message: NormalizedMessage,
        task: TaskRecord,
        reason: str,
        resources: list[Any],
        attempt_count: int,
        last_error: str | None,
    ) -> int:
        return self.approvals.notify_owner(
            task=task,
            reason=reason,
            payload={
                "type": reason,
                "message_id": message.message_id,
                "stage": "resource_download",
                "attempt_count": attempt_count,
                "error": _truncate_error(last_error),
                "statuses": _resource_status_counts(resources),
                "message": "Message resources were not ready; task session agent was not called.",
                "dedupe_key": f"owner-resource-download:{message.message_id}:{reason}",
            },
        )

    def _run_task_router(
        self,
        *,
        message: NormalizedMessage,
        source: str,
        reason: str,
        now: str,
        watch_until: str,
        run_id: str,
    ) -> RoutingResult | ProcessingResult | None:
        active_candidates = self.collector.collect(message, now=now)
        historical = []
        if reason == "closed_recall_router_placeholder":
            historical = self.store.get_related_closed_tasks(message, since=_minus_days(now, 7))
        active_target_short_ids = {candidate.task.short_id for candidate in active_candidates}
        historical_target_short_ids = {task.short_id for task in historical}
        allowed_target_short_ids = active_target_short_ids | historical_target_short_ids
        self.logger.debug(
            "task_router_started",
            run_id=run_id,
            data={
                "message_id": message.message_id,
                "source": source,
                "reason": reason,
                "active_candidates": len(active_candidates),
                "historical_candidates": len(historical),
            },
        )
        prompt = build_router_prompt(
            message=message,
            active=active_candidates,
            historical=historical,
            context_access=self._router_context_access(
                message=message,
                active_candidates=active_candidates,
                historical=historical,
            ),
            message_counts=self._router_message_counts(active_candidates=active_candidates, historical=historical),
        )
        outcome = self._call_agent_with_retries(
            lambda: self.agent_backend.task_router(prompt),
            run_id=run_id,
            stage="task_router",
            message_id=message.message_id,
        )
        result = outcome.result
        self.store.record_agent_audit(
            backend_provider=self.agent_backend.provider,
            request_type="router",
            task_id=None,
            agent_session_id=None if result is None else result.session_id,
            input_message_ids=[message.message_id],
            input_resource_ids=[resource.file_key for resource in message.resources],
            response=result.json_data if result is not None and isinstance(result.json_data, dict) else None,
            error=outcome.last_error if result is None else result.error,
            latency_ms=None if result is None else result.latency_ms,
            prompt={"text": prompt} if self.config.debug.save_full_agent_io else None,
            tool_permissions_profile=self.config.tool_permissions,
        )
        candidates_count = len(active_candidates) + len(historical)
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            last_error = outcome.last_error or (None if result is None else _agent_result_error(result))
            self.logger.error(
                "task_router_failed",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "attempt_count": outcome.attempt_count,
                    "candidates_count": candidates_count,
                    "error": _truncate_error(last_error),
                },
            )
            self._mark_processing_terminal(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="task_router_failed",
            )
            self._audit_router_ambiguity(
                message=message,
                reason="task_router_failed",
                candidates_count=candidates_count,
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=None,
                stage="task_router",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason="task_router_failed",
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_failed")
        try:
            output = TaskRouterOutput.model_validate(result.json_data)
        except ValidationError as exc:
            last_error = str(exc)
            self.logger.error(
                "task_router_schema_failed",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "attempt_count": outcome.attempt_count,
                    "error": _truncate_error(last_error),
                },
            )
            self._mark_processing_terminal(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="task_router_schema_failed",
            )
            self._audit_router_ambiguity(
                message=message,
                reason="task_router_schema_failed",
                candidates_count=candidates_count,
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=None,
                stage="task_router",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason="task_router_schema_failed",
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_schema_failed")
        if output.route == "ambiguous":
            self.logger.warning(
                "task_router_ambiguous",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "reason": output.reason,
                    "candidates_count": candidates_count,
                },
            )
            self.store.record_routing_audit(
                message_id=message.message_id,
                decision=RouteDecision(
                    "ambiguous",
                    reason=output.reason or "task_router_ambiguous",
                    candidates_count=candidates_count,
                    router_called=True,
                ),
            )
            action_id = self.approvals.notify_owner(task=None, reason="task_router_ambiguous", payload={"message_id": message.message_id})
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_ambiguous")
        if output.route == "new_task":
            task, decision = self.store.create_task_for_message_and_audit(
                message,
                watch_until=watch_until,
                reason=output.reason or "task_router_new",
                candidates_count=candidates_count,
                router_called=True,
                matched_by="task_router",
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
            )
            self.logger.info(
                "task_router_decided",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "reason": output.reason or "task_router_new",
                    "task_short_id": task.short_id,
                },
            )
            return RoutingResult(decision=decision, task=task)
        if output.route == "ignore":
            self.store.record_routing_audit(
                message_id=message.message_id,
                decision=RouteDecision(
                    "ignore",
                    reason=output.reason or "task_router_ignore",
                    candidates_count=len(active_candidates) + len(historical),
                    router_called=True,
                ),
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            self.logger.info(
                "task_router_decided",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "reason": output.reason or "task_router_ignore",
                },
            )
            return None
        if output.target_task_id not in allowed_target_short_ids:
            self.logger.warning(
                "task_router_invalid_target",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "target_task_id": output.target_task_id,
                    "allowed_target_count": len(allowed_target_short_ids),
                },
            )
            action_id = self._handle_invalid_router_target(
                message=message,
                target_task_id=output.target_task_id,
                candidates_count=candidates_count,
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_invalid_target")
        target = self._resolve_router_target(output.target_task_id)
        if target is None:
            self.logger.warning(
                "task_router_target_missing",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "target_task_id": output.target_task_id,
                },
            )
            action_id = self._handle_invalid_router_target(
                message=message,
                target_task_id=output.target_task_id,
                candidates_count=candidates_count,
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_invalid_target")
        route_error = _router_target_route_error(
            route=output.route,
            target_task_id=output.target_task_id,
            active_target_short_ids=active_target_short_ids,
            historical_target_short_ids=historical_target_short_ids,
        )
        if route_error is not None:
            self.logger.warning(
                "task_router_invalid_route",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "target_task_id": output.target_task_id,
                    "reason": route_error,
                },
            )
            action_id = self._handle_invalid_router_target(
                message=message,
                target_task_id=output.target_task_id,
                candidates_count=candidates_count,
                reason="task_router_invalid_route",
                payload={"route": output.route, "route_error": route_error},
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_invalid_route")
        if output.route in {"attach_task", "reopen_task"}:
            self.store.attach_message_to_task(target.id, message, watch_until=watch_until)
            decision = RouteDecision(
                output.route,
                target_task_id=target.id,
                target_task_short_id=target.short_id,
                reason=output.reason or "task_router",
                candidates_count=candidates_count,
                router_called=True,
                matched_by="task_router",
            )
            self.store.record_routing_audit(message_id=message.message_id, decision=decision)
            if output.route == "reopen_task":
                self.store.update_task_after_agent(task_id=target.id, status="watching", watch_until=watch_until)
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=target.id,
                attempt_count=outcome.attempt_count,
            )
            self.logger.info(
                "task_router_decided",
                run_id=run_id,
                task_id=str(target.id),
                data={
                    "message_id": message.message_id,
                    "route": output.route,
                    "reason": output.reason or "task_router",
                    "task_short_id": target.short_id,
                },
            )
            return RoutingResult(decision=decision, task=self.store.get_task_by_id(target.id))
        return None

    def _run_task_session(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        now: str,
        watch_until: str,
        run_id: str,
    ) -> ProcessingResult:
        session_id = self.store.get_initialized_agent_session_id(task.id)
        task_message_ids = self.store.list_task_message_ids(task.id)
        prompt_message_ids = self._task_session_prompt_message_ids(
            task=task,
            message=message,
            session_id=session_id,
            task_message_ids=task_message_ids,
        )
        preflight = self._resource_preflight(
            task=task,
            message=message,
            prompt_message_ids=prompt_message_ids,
            run_id=run_id,
        )
        resources = preflight.resources
        if not preflight.allow:
            self.logger.warning(
                "task_session_resource_preflight_blocked",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "reason": preflight.reason,
                    "attempt_count": preflight.attempt_count,
                    "error": _truncate_error(preflight.last_error),
                    "statuses": _resource_status_counts(resources),
                },
            )
            if preflight.reason == "resource_needs_bot":
                self._mark_processing_processed(
                    message=message,
                    stage="resource_download",
                    task_id=task.id,
                    attempt_count=preflight.attempt_count,
                )
            else:
                self._mark_processing_terminal(
                    message=message,
                    stage="resource_download",
                    task_id=task.id,
                    attempt_count=preflight.attempt_count,
                    last_error=preflight.last_error,
                    terminal_reason=preflight.reason,
                )
            action_id = self._notify_resource_blocked(
                message=message,
                task=task,
                reason=preflight.reason,
                resources=resources,
                attempt_count=preflight.attempt_count,
                last_error=preflight.last_error,
            )
            return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason=preflight.reason)
        reply_target_message_ids = _reply_target_message_ids(task=task, current_message_id=message.message_id)
        self.logger.debug(
            "task_session_started",
            run_id=run_id,
            task_id=str(task.id),
            data={
                "message_id": message.message_id,
                "task_short_id": task.short_id,
                "resuming_session": session_id is not None,
                "prompt_message_count": len(prompt_message_ids),
                "resource_count": len(resources),
            },
        )
        output_model = InitialTaskSessionOutput if session_id is None else FollowupTaskSessionOutput
        prompt = build_task_session_prompt(
            task=task,
            current_message_id=message.message_id,
            reply_target_message_ids=reply_target_message_ids,
            messages=self.store.get_messages_by_ids(prompt_message_ids),
            resources=resources,
            output_model=output_model,
            context_metadata=_task_session_context_metadata(
                session_id=session_id,
                included_message_count=len(prompt_message_ids),
                task_message_count=len(task_message_ids) or len(prompt_message_ids),
            ),
            context_access=self._task_session_context_access(message=message, task=task),
        )
        outcome = self._call_agent_with_retries(
            lambda: self.agent_backend.task_session(prompt, session_id=session_id),
            run_id=run_id,
            stage="task_session",
            message_id=message.message_id,
            task_id=task.id,
        )
        result = outcome.result
        self.store.record_agent_audit(
            backend_provider=self.agent_backend.provider,
            request_type="task_session",
            task_id=task.id,
            agent_session_id=session_id if result is None else result.session_id or session_id,
            input_message_ids=prompt_message_ids,
            input_resource_ids=[row["file_key"] for row in resources],
            response=result.json_data if result is not None and isinstance(result.json_data, dict) else None,
            error=outcome.last_error if result is None else result.error,
            latency_ms=None if result is None else result.latency_ms,
            prompt={"text": prompt} if self.config.debug.save_full_agent_io else None,
            tool_permissions_profile=self.config.tool_permissions,
        )
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            last_error = outcome.last_error or (None if result is None else _agent_result_error(result))
            self.logger.error(
                "task_session_failed",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "attempt_count": outcome.attempt_count,
                    "error": _truncate_error(last_error),
                },
            )
            self._mark_processing_terminal(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="agent_task_session_failed",
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=task,
                stage="task_session",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason="agent_task_session_failed",
            )
            return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason="agent_failed")
        try:
            output = output_model.model_validate(result.json_data)
        except ValidationError as exc:
            last_error = str(exc)
            self.logger.error(
                "task_session_schema_failed",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "attempt_count": outcome.attempt_count,
                    "error": _truncate_error(last_error),
                },
            )
            self._mark_processing_terminal(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="agent_schema_failed",
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=task,
                stage="task_session",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason=f"agent_schema_failed: {exc.errors()[0]['msg']}",
            )
            return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason="schema_failed")
        if result.session_id and result.session_id != session_id:
            self.store.set_task_agent_session_id(task.id, result.session_id)
            self.logger.debug(
                "task_session_id_updated",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "had_previous_session": session_id is not None,
                },
            )

        target_ids = {message.message_id}
        if task.root_message_id:
            target_ids.add(task.root_message_id)
        if output.reply_target_message_id and output.reply_target_message_id not in target_ids:
            self.logger.warning(
                "task_session_invalid_reply_target",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "reply_target_message_id": output.reply_target_message_id,
                    "allowed_targets": sorted(target_ids),
                },
            )
            fallback_target = self.store.get_message(message.message_id)
            composed = self.composer.compose(
                proposed_reply=output.proposed_reply,
                reply_target=fallback_target,
                chat_type=task.chat_type or message.chat_type,
            )
            approval_id = self.approvals.request_send_reply(
                task=task,
                reply_target_message_id=message.message_id,
                proposed_reply=output.proposed_reply,
                final_reply=composed.text,
                reason="invalid_reply_target_message_id",
                approvable=_can_directly_approve(output.proposed_reply, composed),
            )
            self._mark_processing_processed(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("approval_created", task.id, approval_id=approval_id, reason="invalid_reply_target")

        next_status = "closed" if output.watch_action == "close" else "watching"
        self.store.update_task_after_agent(
            task_id=task.id,
            task_label=output.task_label if isinstance(output, InitialTaskSessionOutput) else None,
            status=next_status,
            watch_until=watch_until if next_status == "watching" else None,
        )
        if output.answerability == "no_reply":
            self._mark_processing_processed(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
            )
            self.logger.info(
                "task_session_watch_only",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "watch_action": output.watch_action,
                },
            )
            return ProcessingResult("watch_only", task.id, reason="no_reply")

        reply_target_id = output.reply_target_message_id or message.message_id
        reply_target = self.store.get_message(reply_target_id)
        composed = self.composer.compose(
            proposed_reply=output.proposed_reply,
            reply_target=reply_target,
            chat_type=task.chat_type or message.chat_type,
        )
        gate = self._reply_gate(
            task=task,
            message=message,
            output=output,
            composed=composed,
        )
        if not gate["allow"]:
            self.logger.warning(
                "task_session_auto_reply_blocked",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "reason": gate["reason"],
                    "identity": gate["identity"],
                    "answerability": output.answerability,
                },
            )
            approval_id = self.approvals.request_send_reply(
                task=task,
                reply_target_message_id=reply_target_id,
                proposed_reply=output.proposed_reply,
                final_reply=composed.text,
                reason=gate["reason"],
                approvable=_can_directly_approve(output.proposed_reply, composed),
            )
            self._mark_processing_processed(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("approval_created", task.id, approval_id=approval_id, reason=gate["reason"])
        payload = {
            "reply_target_message_id": reply_target_id,
            "text": composed.text,
            "identity": gate["identity"],
            "source": "auto_reply",
        }
        action_id = self.store.create_send_reply_action(
            task_id=task.id,
            target_message_id=reply_target_id,
            payload=payload,
        )
        self._mark_processing_processed(
            message=message,
            stage="task_session",
            task_id=task.id,
            attempt_count=outcome.attempt_count,
        )
        self.logger.info(
            "task_session_auto_reply_ready",
            run_id=run_id,
            task_id=str(task.id),
            data={
                "message_id": message.message_id,
                "task_short_id": task.short_id,
                "action_id": action_id,
                "reply_target_message_id": reply_target_id,
                "identity": gate["identity"],
            },
        )
        return ProcessingResult("send_action_created", task.id, action_id=action_id, reason="gate_passed")

    def _resource_preflight(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        prompt_message_ids: list[str],
        run_id: str,
    ) -> ResourcePreflightResult:
        resources = self.store.list_resources_for_messages(prompt_message_ids)
        state = _resource_preflight_state(resources, message=message, prompt_message_ids=prompt_message_ids)
        if state["allow"]:
            return ResourcePreflightResult(True, "ok", resources)
        attempt_count = _initial_resource_attempt_count(resources, message=message, prompt_message_ids=prompt_message_ids)
        last_error = state["error"]
        if state["retryable"] and self.resource_retry_func is not None and _has_current_prompt_resources(
            message=message,
            prompt_message_ids=prompt_message_ids,
        ):
            for attempt in range(attempt_count + 1, RESOURCE_MAX_ATTEMPTS + 1):
                attempt_count = attempt
                self.logger.debug(
                    "resource_download_retry_started",
                    run_id=run_id,
                    task_id=str(task.id),
                    data={
                        "message_id": message.message_id,
                        "task_short_id": task.short_id,
                        "attempt": attempt,
                        "reason": state["reason"],
                    },
                )
                try:
                    retry_error = None
                    self.resource_retry_func(message, run_id)
                except Exception as exc:
                    retry_error = f"{type(exc).__name__}: {exc}"
                    last_error = retry_error
                    self.logger.warning(
                        "resource_download_retry_exception",
                        run_id=run_id,
                        task_id=str(task.id),
                        data={
                            "message_id": message.message_id,
                            "task_short_id": task.short_id,
                            "attempt": attempt,
                            "error": _truncate_error(retry_error),
                        },
                    )
                resources = self.store.list_resources_for_messages(prompt_message_ids)
                state = _resource_preflight_state(resources, message=message, prompt_message_ids=prompt_message_ids)
                if retry_error is None and not state["allow"] and state["error"] is not None:
                    last_error = state["error"]
                if state["allow"]:
                    return ResourcePreflightResult(True, "ok", resources, attempt_count=attempt_count)
                if not state["retryable"]:
                    break
                if attempt < RESOURCE_MAX_ATTEMPTS:
                    self._sleep_before_retry(attempt)
        reason = state["reason"]
        if state["retryable"]:
            reason = "resource_download_failed"
            last_error = last_error or state["error"] or _resource_status_error(resources)
        return ResourcePreflightResult(
            False,
            reason,
            resources,
            attempt_count=attempt_count,
            last_error=last_error or state["error"] or _resource_status_error(resources),
        )

    def _reply_gate(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        output: BaseTaskSessionOutput,
        composed: ComposedReply,
    ) -> dict[str, Any]:
        if output.answerability != "auto_reply":
            return {"allow": False, "reason": "needs_owner", "identity": "user"}
        if composed.had_forbidden_mentions:
            return {"allow": False, "reason": "forbidden_mentions", "identity": "user"}
        if not output.proposed_reply.strip() or not composed.text.strip():
            return {"allow": False, "reason": "empty_proposed_reply", "identity": "user"}
        chat_type = task.chat_type or message.chat_type
        policy = self._chat_policy(task.chat_id or message.chat_id)
        if chat_type == "p2p":
            if not self.config.reply_policy.p2p_auto_reply:
                return {"allow": False, "reason": "p2p_auto_reply_disabled", "identity": "user"}
            return {"allow": True, "reason": "ok", "identity": "user"}
        if chat_type == "group":
            if not message.direct_mention:
                return {"allow": False, "reason": "group_not_direct_mention", "identity": "user"}
            chat_configured = bool((task.chat_id or message.chat_id) in self.config.chats)
            if not chat_configured:
                # Unknown groups are still processed for owner visibility, but
                # never auto-replied until a per-chat policy exists.
                return {"allow": False, "reason": "unknown_group_auto_reply_disabled", "identity": "user"}
            if not policy.auto_reply:
                return {"allow": False, "reason": "group_auto_reply_disabled", "identity": "user"}
            if policy.reply_identity in {"bot", "bot_preferred"} and policy.bot_joined:
                return {"allow": True, "reason": "ok", "identity": "bot"}
            if policy.reply_identity == "user":
                return {"allow": True, "reason": "ok", "identity": "user"}
            if policy.reply_identity == "bot_preferred" and policy.allow_user_fallback:
                return {"allow": True, "reason": "ok", "identity": "user"}
            return {"allow": False, "reason": "bot_not_joined", "identity": "bot"}
        return {"allow": False, "reason": "unknown_chat_type", "identity": "user"}

    def _chat_policy(self, chat_id: str | None) -> ChatPolicyConfig:
        if chat_id and chat_id in self.config.chats:
            return self.config.chats[chat_id]
        return ChatPolicyConfig()

    def _task_session_prompt_message_ids(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        session_id: str | None,
        task_message_ids: list[str] | None = None,
    ) -> list[str]:
        if session_id is not None:
            # Resumed agent sessions already carry task history; sending only
            # the current message keeps follow-up prompts compact and bounded.
            return [message.message_id]
        message_ids = task_message_ids if task_message_ids is not None else self.store.list_task_message_ids(task.id)
        return message_ids or [message.message_id]

    def _router_context_access(
        self,
        *,
        message: NormalizedMessage,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[str, Any] | None:
        context = self._base_context_access()
        if context is None:
            return None
        context["query_scope"] = {
            "current_message_id": message.message_id,
            "active_tasks": [_context_task_card(candidate.task) for candidate in active_candidates],
            "historical_tasks": [_context_task_card(task) for task in historical],
        }
        return context

    def _router_message_counts(
        self,
        *,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[int, int]:
        task_ids = [candidate.task.id for candidate in active_candidates] + [task.id for task in historical]
        return self.store.count_task_messages_by_task_ids(task_ids)

    def _task_session_context_access(
        self,
        *,
        message: NormalizedMessage,
        task: TaskRecord,
    ) -> dict[str, Any] | None:
        context = self._base_context_access()
        if context is None:
            return None
        context["query_scope"] = {
            "current_message_id": message.message_id,
            "task": _context_task_card(task),
        }
        return context

    def _base_context_access(self) -> dict[str, Any] | None:
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

    def _resolve_router_target(self, target_task_id: str | None) -> TaskRecord | None:
        if not target_task_id:
            return None
        return self.store.get_task_by_short_id(target_task_id)

    def _handle_invalid_router_target(
        self,
        *,
        message: NormalizedMessage,
        target_task_id: str | None,
        candidates_count: int,
        reason: str = "task_router_invalid_target",
        payload: dict[str, Any] | None = None,
    ) -> int:
        notification_payload = {"message_id": message.message_id, "target": target_task_id}
        if payload:
            notification_payload.update(payload)
        action_id = self.approvals.notify_owner(
            task=None,
            reason=reason,
            payload=notification_payload,
        )
        self._audit_router_ambiguity(
            message=message,
            reason=reason,
            candidates_count=candidates_count,
        )
        return action_id

    def _audit_router_ambiguity(
        self,
        *,
        message: NormalizedMessage,
        reason: str,
        candidates_count: int,
    ) -> None:
        self.store.record_routing_audit(
            message_id=message.message_id,
            decision=RouteDecision(
                "ambiguous",
                reason=reason,
                candidates_count=candidates_count,
                router_called=True,
            ),
        )


def _router_target_route_error(
    *,
    route: str,
    target_task_id: str | None,
    active_target_short_ids: set[str],
    historical_target_short_ids: set[str],
) -> str | None:
    if route == "attach_task" and target_task_id not in active_target_short_ids:
        return "attach_task_requires_active_target"
    if route == "reopen_task" and target_task_id not in historical_target_short_ids:
        return "reopen_task_requires_historical_target"
    return None


def _can_directly_approve(proposed_reply: str, composed: ComposedReply) -> bool:
    return bool(proposed_reply.strip()) and bool(composed.text.strip()) and not composed.had_forbidden_mentions


def _context_task_card(task: TaskRecord) -> dict[str, Any]:
    return {"id": task.id, "short_id": task.short_id}


def _task_session_context_metadata(
    *,
    session_id: str | None,
    included_message_count: int,
    task_message_count: int,
) -> dict[str, Any]:
    history_carried = session_id is not None
    return {
        "message_context_mode": "incremental_current_message" if history_carried else "full_task_messages",
        "included_message_count": included_message_count,
        "task_message_count": task_message_count,
        "history_carried_by_agent_session": history_carried,
    }


def _escape_mention_display(value: str) -> str:
    escaped = escape(value, quote=False)
    return (
        escaped.replace("@所有人", "&#64;所有人")
        .replace("@_all", "&#64;_all")
        .replace("@all", "&#64;all")
    )


def _reply_target_message_ids(*, task: TaskRecord, current_message_id: str) -> list[str]:
    ids = [current_message_id]
    if task.root_message_id:
        ids.append(task.root_message_id)
    return list(dict.fromkeys(ids))


def _resource_preflight_state(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> dict[str, Any]:
    missing_current = _missing_current_prompt_resources(
        resources,
        message=message,
        prompt_message_ids=prompt_message_ids,
    )
    if missing_current:
        return {
            "allow": False,
            "reason": "resource_missing",
            "retryable": True,
            "error": f"missing resource records: {', '.join(missing_current)}",
        }
    if not resources:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    statuses = {row["download_status"] for row in resources}
    if not statuses:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    if statuses <= {"downloaded"}:
        return {"allow": True, "reason": "ok", "retryable": False, "error": None}
    if statuses & {"bot_not_joined", "bot_invisible"}:
        return {
            "allow": False,
            "reason": "resource_needs_bot",
            "retryable": False,
            "error": _resource_status_error(resources),
        }
    if statuses & {"skipped"}:
        return {
            "allow": False,
            "reason": "resource_download_disabled",
            "retryable": False,
            "error": _resource_status_error(resources),
        }
    return {
        "allow": False,
        "reason": "resource_download_failed",
        "retryable": True,
        "error": _resource_status_error(resources),
    }


def _initial_resource_attempt_count(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> int:
    if not _has_current_prompt_resources(message=message, prompt_message_ids=prompt_message_ids):
        return 0
    current_keys = {
        (resource.message_id, resource.file_key, resource.resource_type)
        for resource in message.resources
    }
    row_keys = {
        (row["message_id"], row["file_key"], row["resource_type"])
        for row in resources
    }
    return 1 if current_keys & row_keys else 0


def _has_current_prompt_resources(*, message: NormalizedMessage, prompt_message_ids: list[str]) -> bool:
    return message.message_id in set(prompt_message_ids) and bool(message.resources)


def _missing_current_prompt_resources(
    resources: list[Any],
    *,
    message: NormalizedMessage,
    prompt_message_ids: list[str],
) -> list[str]:
    if not _has_current_prompt_resources(message=message, prompt_message_ids=prompt_message_ids):
        return []
    row_keys = {
        (row["message_id"], row["file_key"], row["resource_type"])
        for row in resources
    }
    missing: list[str] = []
    for resource in message.resources:
        key = (resource.message_id, resource.file_key, resource.resource_type)
        if key not in row_keys:
            missing.append(f"{resource.resource_type}:{resource.file_key}")
    return missing


def _resource_status_counts(resources: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in resources:
        status = str(row["download_status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def _resource_status_error(resources: list[Any]) -> str | None:
    counts = _resource_status_counts(resources)
    if not counts:
        return None
    return "resource statuses: " + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def _agent_result_error(result: AgentRunResult) -> str:
    parts = [
        result.error,
        result.stderr,
        result.stdout,
        f"exit_code={result.exit_code}" if result.exit_code is not None else None,
        "timed_out=True" if result.timed_out else None,
    ]
    return " ".join(str(part).strip() for part in parts if part).strip() or "Agent backend call failed"


def _is_retryable_agent_result(result: AgentRunResult) -> bool:
    error_text = _agent_result_error(result).lower()
    if result.timed_out:
        return True
    if any(marker in error_text for marker in TERMINAL_AGENT_ERROR_MARKERS):
        return False
    if "stdout was not valid json" in error_text:
        return True
    if result.exit_code is None:
        return True
    return result.exit_code != 0


def _truncate_error(value: str | None, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit - 3]}..."


def _plus_minutes(value: str, minutes: int) -> str:
    try:
        base = datetime.fromisoformat(value)
    except ValueError:
        base = datetime.now().astimezone()
    return (base + timedelta(minutes=minutes)).astimezone().isoformat(timespec="seconds")


def _minus_days(value: str, days: int) -> str:
    try:
        base = datetime.fromisoformat(value)
    except ValueError:
        base = datetime.now().astimezone()
    return (base - timedelta(days=days)).astimezone().isoformat(timespec="seconds")
