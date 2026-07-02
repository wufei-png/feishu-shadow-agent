from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Callable

from pydantic import ValidationError

from .agent_backend import AgentBackend
from .agent_invocation import AGENT_MAX_ATTEMPTS, AgentAttemptOutcome, AgentInvoker, agent_result_error, truncate_error
from .config import AppConfig
from .context_access import ContextAccessBuilder
from .jsonl import JSONLLogger
from .policy import PolicyResolver
from .prompt import (
    BaseTaskSessionOutput,
    InitialTaskSessionOutput,
    TaskRouterOutput,
    build_router_prompt,
)
from .resource_preflight import ResourcePreflight, ResourcePreflightResult, resource_status_counts
from .routing import CandidateCollector, RoutingResult
from .store.sqlite_store import SQLiteStore
from .task_session_runner import TaskSessionRunner
from .types import LifecycleStatePolicy, MessageProcessingStatus, NormalizedMessage, RouteDecision, TaskRecord

AGENT_AT_SPAN_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
FORBIDDEN_MENTION_RE = re.compile(r"<at\b[^>]*>|</at>|@所有人|@_all|@all", re.IGNORECASE)


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
        incoming_message_id: str | None = None,
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
        current_task = self.store.get_task_by_id(task.id)
        notification_message_id = incoming_message_id or reply_target_message_id
        source_message = self.store.get_message(notification_message_id)
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
            "source": _notification_source(task=current_task, message=source_message),
            "incoming_message": _notification_message(source_message, fallback_message_id=notification_message_id),
            "suggested_reply": payload_text,
            "approvable": approvable,
            "commands": commands,
        }
        if notification_message_id != reply_target_message_id:
            notify["reply_target_message_id"] = reply_target_message_id
        return self.store.create_send_reply_approval(
            task_id=task.id,
            preview=proposed_reply,
            payload=payload,
            notify_payload=notify,
            approval_timeout_hours=self.config.lifecycle.approval_timeout_hours,
        )

    def notify_owner(self, *, task: TaskRecord | None, reason: str, payload: dict[str, Any] | None = None) -> int:
        data = {"type": "owner_notification", "reason": reason} | (payload or {})
        current_task = self.store.get_task_by_id(task.id) if task is not None else None
        message_id = data.get("message_id")
        source_message = self.store.get_message(message_id) if isinstance(message_id, str) and message_id else None
        if task is not None:
            data["task_id"] = task.short_id
        if "incoming_message" not in data and isinstance(message_id, str) and message_id:
            data["incoming_message"] = _notification_message(source_message, fallback_message_id=message_id)
        if "source" not in data:
            source = _notification_source(task=current_task, message=source_message)
            if any(value for value in source.values()):
                data["source"] = source
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
        self.policy = PolicyResolver(store)
        self.agent_invoker = AgentInvoker(
            logger=logger,
            max_attempts=agent_max_attempts,
            retry_delays_seconds=agent_retry_delays_seconds,
            sleep_func=sleep_func,
        )
        self.context_access = ContextAccessBuilder(store=store, config=config)
        self.resource_preflight = ResourcePreflight(
            store=store,
            logger=logger,
            retry_delays_seconds=agent_retry_delays_seconds,
            sleep_func=sleep_func,
        )
        self.task_sessions = TaskSessionRunner(
            store=store,
            agent_backend=agent_backend,
            agent_invoker=self.agent_invoker,
            context_access=self.context_access,
        )

    def set_resource_retry_func(self, func: Callable[[NormalizedMessage, str | None], None]) -> None:
        self.resource_preflight.set_retry_func(func)

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
        call: Callable[[], Any],
        *,
        run_id: str | None,
        stage: str,
        message_id: str,
        task_id: int | None = None,
    ) -> AgentAttemptOutcome:
        return self.agent_invoker.call_with_retries(
            call,
            run_id=run_id,
            stage=stage,
            message_id=message_id,
            task_id=task_id,
        )

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
            last_error=truncate_error(last_error),
            terminal_reason=terminal_reason,
        )

    def _mark_processing_blocked_external(
        self,
        *,
        message: NormalizedMessage,
        stage: str,
        task_id: int | None,
        attempt_count: int,
        last_error: str | None,
        reason: str,
    ) -> None:
        self.store.record_message_processing(
            message_id=message.message_id,
            task_id=task_id,
            stage=stage,
            status=MessageProcessingStatus.BLOCKED_WAITING_EXTERNAL.value,
            attempt_count=attempt_count,
            last_error=truncate_error(last_error),
            terminal_reason=reason,
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
                "error": truncate_error(last_error),
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
                "error": truncate_error(last_error),
                "statuses": resource_status_counts(resources),
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
            historical = self.store.get_related_closed_tasks(
                message,
                since=_minus_days(now, self.config.lifecycle.closed_recall_days),
            )
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
            last_error = outcome.last_error or (None if result is None else agent_result_error(result))
            self.logger.error(
                "task_router_failed",
                run_id=run_id,
                data={
                    "message_id": message.message_id,
                    "attempt_count": outcome.attempt_count,
                    "candidates_count": candidates_count,
                    "error": truncate_error(last_error),
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
                    "error": truncate_error(last_error),
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
        session_plan = self.task_sessions.build_plan(task=task, message=message)
        preflight = self._resource_preflight(
            task=task,
            message=message,
            prompt_message_ids=session_plan.prompt_message_ids,
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
                    "error": truncate_error(preflight.last_error),
                    "statuses": resource_status_counts(resources),
                },
            )
            processing_status = LifecycleStatePolicy.resource_blocker_status(preflight.reason)
            if processing_status == MessageProcessingStatus.BLOCKED_WAITING_EXTERNAL.value:
                self._mark_processing_blocked_external(
                    message=message,
                    stage="resource_download",
                    task_id=task.id,
                    attempt_count=preflight.attempt_count,
                    last_error=preflight.last_error,
                    reason=preflight.reason,
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
        self.logger.debug(
            "task_session_started",
            run_id=run_id,
            task_id=str(task.id),
            data={
                "message_id": message.message_id,
                "task_short_id": task.short_id,
                "resuming_session": session_plan.session_id is not None,
                "prompt_message_count": len(session_plan.prompt_message_ids),
                "resource_count": len(resources),
            },
        )
        session_run = self.task_sessions.run(
            task=task,
            message=message,
            plan=session_plan,
            resources=resources,
            run_id=run_id,
        )
        outcome = session_run.outcome
        result = outcome.result
        self.store.record_agent_audit(
            backend_provider=self.agent_backend.provider,
            request_type="task_session",
            task_id=task.id,
            agent_session_id=session_plan.session_id if result is None else result.session_id or session_plan.session_id,
            input_message_ids=session_plan.prompt_message_ids,
            input_resource_ids=[row["file_key"] for row in resources],
            response=result.json_data if result is not None and isinstance(result.json_data, dict) else None,
            error=outcome.last_error if result is None else result.error,
            latency_ms=None if result is None else result.latency_ms,
            prompt={"text": session_run.prompt} if self.config.debug.save_full_agent_io else None,
            tool_permissions_profile=self.config.tool_permissions,
        )
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            last_error = outcome.last_error or (None if result is None else agent_result_error(result))
            self.logger.error(
                "task_session_failed",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "attempt_count": outcome.attempt_count,
                    "error": truncate_error(last_error),
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
        if session_run.validation_error is not None:
            exc = session_run.validation_error
            last_error = str(exc)
            self.logger.error(
                "task_session_schema_failed",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "attempt_count": outcome.attempt_count,
                    "error": truncate_error(last_error),
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
        output = session_run.output
        if output is None:
            raise RuntimeError("task session runner returned no output after successful validation")
        if result.session_id and result.session_id != session_plan.session_id:
            self.store.set_task_agent_session_id(task.id, result.session_id)
            self.logger.debug(
                "task_session_id_updated",
                run_id=run_id,
                task_id=str(task.id),
                data={
                    "message_id": message.message_id,
                    "task_short_id": task.short_id,
                    "had_previous_session": session_plan.session_id is not None,
                },
            )

        target_ids = set(session_plan.reply_target_message_ids)
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
            self.store.update_task_after_agent(
                task_id=task.id,
                task_label=output.task_label if isinstance(output, InitialTaskSessionOutput) else None,
                status="watching",
                watch_until=watch_until,
            )
            approval_id = self.approvals.request_send_reply(
                task=task,
                reply_target_message_id=message.message_id,
                incoming_message_id=message.message_id,
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

        if output.answerability == "no_reply":
            next_status = "closed" if output.watch_action == "close" else "watching"
            self.store.update_task_after_agent(
                task_id=task.id,
                task_label=output.task_label if isinstance(output, InitialTaskSessionOutput) else None,
                status=next_status,
                watch_until=watch_until if next_status == "watching" else None,
            )
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
                    "policy_source": gate["policy_source"],
                    "answerability": output.answerability,
                },
            )
            self.store.update_task_after_agent(
                task_id=task.id,
                task_label=output.task_label if isinstance(output, InitialTaskSessionOutput) else None,
                status="watching",
                watch_until=watch_until,
            )
            approval_id = self.approvals.request_send_reply(
                task=task,
                reply_target_message_id=reply_target_id,
                incoming_message_id=message.message_id,
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
            "policy_source": gate["policy_source"],
        }
        next_status = "closed" if output.watch_action == "close" else "watching"
        self.store.update_task_after_agent(
            task_id=task.id,
            task_label=output.task_label if isinstance(output, InitialTaskSessionOutput) else None,
            status=next_status,
            watch_until=watch_until if next_status == "watching" else None,
        )
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
                "policy_source": gate["policy_source"],
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
        return self.resource_preflight.check(
            task=task,
            message=message,
            prompt_message_ids=prompt_message_ids,
            run_id=run_id,
        )

    def _reply_gate(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        output: BaseTaskSessionOutput,
        composed: ComposedReply,
    ) -> dict[str, Any]:
        decision = self.policy.resolve_reply_policy(
            task=task,
            message=message,
            answerability=output.answerability,
            had_forbidden_mentions=composed.had_forbidden_mentions,
            proposed_reply=output.proposed_reply,
            final_reply=composed.text,
        )
        return {
            "allow": decision.allow,
            "reason": decision.reason,
            "identity": decision.identity,
            "policy_source": decision.policy_source,
        }

    def _router_context_access(
        self,
        *,
        message: NormalizedMessage,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[str, Any] | None:
        return self.context_access.router_context_access(
            message=message,
            active_candidates=active_candidates,
            historical=historical,
        )

    def _router_message_counts(
        self,
        *,
        active_candidates: list[Any],
        historical: list[TaskRecord],
    ) -> dict[int, int]:
        return self.context_access.router_message_counts(
            active_candidates=active_candidates,
            historical=historical,
        )

    def _base_context_access(self) -> dict[str, Any] | None:
        return self.context_access.base_context_access()

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


def _notification_source(*, task: TaskRecord | None, message: Any | None) -> dict[str, Any]:
    return {
        "task_label": None if task is None else task.task_label,
        "chat_id": _row_value(message, "chat_id") or (None if task is None else task.chat_id),
        "chat_type": _row_value(message, "chat_type") or (None if task is None else task.chat_type),
        "sender_name": _row_value(message, "sender_name"),
        "sender_id": _row_value(message, "sender_id"),
        "sent_at": _row_value(message, "sent_at"),
    }


def _notification_message(message: Any | None, *, fallback_message_id: str) -> dict[str, Any]:
    return {
        "message_id": _row_value(message, "message_id") or fallback_message_id,
        "text": _row_value(message, "text") or "",
    }


def _row_value(row: Any | None, key: str) -> Any | None:
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _escape_mention_display(value: str) -> str:
    escaped = escape(value, quote=False)
    return (
        escaped.replace("@所有人", "&#64;所有人")
        .replace("@_all", "&#64;_all")
        .replace("@all", "&#64;all")
    )


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
