from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import AppConfig, ChatPolicyConfig
from .hermes import HermesClient
from .jsonl import JSONLLogger
from .routing import CandidateCollector, RoutingResult
from .store.sqlite_store import SQLiteStore
from .types import HermesCliResult, NormalizedMessage, RouteDecision, TaskRecord

WATCH_EXTEND_MINUTES = 120
HERMES_MAX_ATTEMPTS = 3
HERMES_AT_SPAN_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
FORBIDDEN_MENTION_RE = re.compile(r"<at\b[^>]*>|</at>|@所有人|@_all|@all", re.IGNORECASE)
WATCH_KEY_RE = re.compile(r"^(?:user|msg|thread):[^\s:]+$")
TERMINAL_HERMES_ERROR_MARKERS = (
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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRouterOutput(StrictModel):
    route: Literal["new_task", "attach_task", "reopen_task", "close_task", "ignore", "ambiguous"]
    target_task_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
    updated_watch_keys: list[str] = Field(default_factory=list)


class TaskSessionOutput(StrictModel):
    task_label: str
    task_state: Literal["needs_reply", "watching", "closed", "waiting"]
    answerability: Literal["auto_reply", "needs_owner", "no_reply"]
    confidence: float = Field(ge=0, le=1)
    proposed_reply: str = ""
    reply_target_message_id: str | None = None
    watch_action: Literal["keep_watching", "close"] = "keep_watching"
    watch_extend_minutes: int = Field(default=WATCH_EXTEND_MINUTES, ge=0, le=24 * 60)
    risk_level: Literal["low", "medium", "high"] = "low"
    safety_notes: list[str] = Field(default_factory=list)
    requires_resources: bool = False

    @field_validator("task_label")
    @classmethod
    def trim_label(cls, value: str) -> str:
        return " ".join(value.split())[:100]


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
class HermesAttemptOutcome:
    result: HermesCliResult | None
    attempt_count: int
    last_error: str | None = None


class SendComposer:
    def __init__(self, *, owner_open_id: str):
        self.owner_open_id = owner_open_id

    def compose(
        self,
        *,
        proposed_reply: str,
        reply_target: Any,
        chat_type: str | None,
    ) -> ComposedReply:
        had_forbidden = bool(HERMES_AT_SPAN_RE.search(proposed_reply) or FORBIDDEN_MENTION_RE.search(proposed_reply))
        cleaned = HERMES_AT_SPAN_RE.sub("", proposed_reply)
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
        hermes_client: HermesClient,
        logger: JSONLLogger,
        hermes_max_attempts: int = HERMES_MAX_ATTEMPTS,
        hermes_retry_delays_seconds: tuple[float, ...] = (1.0, 3.0),
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.config = config
        self.hermes = hermes_client
        self.logger = logger
        self.collector = CandidateCollector(store)
        self.approvals = ApprovalService(store=store, config=config)
        self.composer = SendComposer(owner_open_id=config.owner.open_id)
        self.hermes_max_attempts = max(1, hermes_max_attempts)
        self.hermes_retry_delays_seconds = hermes_retry_delays_seconds
        self.sleep_func = sleep_func

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
        if route in {"ignore", "human_taken_over", "close_task"}:
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

    def _call_hermes_with_retries(self, call: Callable[[], HermesCliResult]) -> HermesAttemptOutcome:
        last_result: HermesCliResult | None = None
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self.hermes_max_attempts + 1):
            attempts = attempt
            try:
                result = call()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.hermes_max_attempts:
                    self._sleep_before_retry(attempt)
                continue
            last_result = result
            if result.ok:
                return HermesAttemptOutcome(result=result, attempt_count=attempt)
            last_error = _hermes_result_error(result)
            if not _is_retryable_hermes_result(result):
                return HermesAttemptOutcome(result=result, attempt_count=attempt, last_error=last_error)
            if attempt < self.hermes_max_attempts:
                self._sleep_before_retry(attempt)
        return HermesAttemptOutcome(result=last_result, attempt_count=attempts, last_error=last_error)

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt <= 0 or not self.hermes_retry_delays_seconds:
            return
        index = min(attempt - 1, len(self.hermes_retry_delays_seconds) - 1)
        delay = self.hermes_retry_delays_seconds[index]
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
                "message": "Hermes processing failed; no reply was generated.",
                "dedupe_key": f"owner-processing-failed:{message.message_id}:{stage}",
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
        allowed_target_short_ids = {candidate.task.short_id for candidate in active_candidates} | {
            task.short_id for task in historical
        }
        prompt = _router_prompt(message=message, active=active_candidates, historical=historical)
        outcome = self._call_hermes_with_retries(lambda: self.hermes.task_router(prompt))
        result = outcome.result
        self.store.record_hermes_audit(
            request_type="router",
            task_id=None,
            hermes_session_id=None if result is None else result.session_id,
            input_message_ids=[message.message_id],
            input_resource_ids=[resource.file_key for resource in message.resources],
            response=result.json_data if result is not None and isinstance(result.json_data, dict) else None,
            error=outcome.last_error if result is None else result.error,
            latency_ms=None if result is None else result.latency_ms,
            prompt={"text": prompt} if self.config.debug.save_full_hermes_io else None,
            tool_permissions_profile=self.config.tool_permissions,
        )
        candidates_count = len(active_candidates) + len(historical)
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            last_error = outcome.last_error or (None if result is None else _hermes_result_error(result))
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
        invalid_watch_keys = _invalid_watch_keys(output.updated_watch_keys)
        if invalid_watch_keys:
            self._audit_router_ambiguity(
                message=message,
                reason="task_router_invalid_watch_keys",
                candidates_count=candidates_count,
            )
            action_id = self.approvals.notify_owner(
                task=None,
                reason="task_router_invalid_watch_keys",
                payload={"message_id": message.message_id, "invalid_watch_keys": invalid_watch_keys},
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=None,
                attempt_count=outcome.attempt_count,
            )
            return ProcessingResult("owner_notification_created", action_id=action_id, reason="task_router_invalid_watch_keys")
        if output.confidence < 0.6 or output.route == "ambiguous":
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
            self.store.add_task_watch_keys(task.id, output.updated_watch_keys)
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
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
            return None
        if output.target_task_id not in allowed_target_short_ids:
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
        if output.route in {"attach_task", "reopen_task"}:
            self.store.attach_message_to_task(target.id, message, watch_until=watch_until)
            self.store.add_task_watch_keys(target.id, output.updated_watch_keys)
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
                self.store.update_task_after_hermes(task_id=target.id, status="watching", watch_until=watch_until)
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=target.id,
                attempt_count=outcome.attempt_count,
            )
            return RoutingResult(decision=decision, task=self.store.get_task_by_id(target.id))
        if output.route == "close_task":
            self.store.update_task_after_hermes(task_id=target.id, status="closed")
            self.store.record_routing_audit(
                message_id=message.message_id,
                decision=RouteDecision(
                    "close_task",
                    target_task_id=target.id,
                    target_task_short_id=target.short_id,
                    reason=output.reason or "task_router_close",
                    router_called=True,
                ),
            )
            self._mark_processing_processed(
                message=message,
                stage="task_router",
                task_id=target.id,
                attempt_count=outcome.attempt_count,
            )
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
        session_id = self.store.get_initialized_hermes_session_id(task.id)
        prompt_message_ids = self._task_session_prompt_message_ids(task=task, message=message, session_id=session_id)
        resources = self.store.list_resources_for_messages(prompt_message_ids)
        reply_target_message_ids = _reply_target_message_ids(task=task, current_message_id=message.message_id)
        prompt = _task_session_prompt(
            task=task,
            current_message_id=message.message_id,
            reply_target_message_ids=reply_target_message_ids,
            messages=self.store.get_messages_by_ids(prompt_message_ids),
            resources=resources,
        )
        outcome = self._call_hermes_with_retries(lambda: self.hermes.task_session(prompt, session_id=session_id))
        result = outcome.result
        self.store.record_hermes_audit(
            request_type="task_session",
            task_id=task.id,
            hermes_session_id=session_id if result is None else result.session_id or session_id,
            input_message_ids=prompt_message_ids,
            input_resource_ids=[row["file_key"] for row in resources],
            response=result.json_data if result is not None and isinstance(result.json_data, dict) else None,
            error=outcome.last_error if result is None else result.error,
            latency_ms=None if result is None else result.latency_ms,
            prompt={"text": prompt} if self.config.debug.save_full_hermes_io else None,
            tool_permissions_profile=self.config.tool_permissions,
        )
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            last_error = outcome.last_error or (None if result is None else _hermes_result_error(result))
            self._mark_processing_terminal(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="hermes_task_session_failed",
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=task,
                stage="task_session",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason="hermes_task_session_failed",
            )
            return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason="hermes_failed")
        try:
            output = TaskSessionOutput.model_validate(result.json_data)
        except ValidationError as exc:
            last_error = str(exc)
            self._mark_processing_terminal(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                terminal_reason="hermes_schema_failed",
            )
            action_id = self._notify_processing_failed(
                message=message,
                task=task,
                stage="task_session",
                attempt_count=outcome.attempt_count,
                last_error=last_error,
                reason=f"hermes_schema_failed: {exc.errors()[0]['msg']}",
            )
            return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason="schema_failed")
        if result.session_id and result.session_id != session_id:
            self.store.set_task_hermes_session_id(task.id, result.session_id)

        target_ids = {message.message_id}
        if task.root_message_id:
            target_ids.add(task.root_message_id)
        if output.reply_target_message_id and output.reply_target_message_id not in target_ids:
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

        next_status = "closed" if output.watch_action == "close" or output.task_state == "closed" else "watching"
        self.store.update_task_after_hermes(
            task_id=task.id,
            task_label=output.task_label,
            status=next_status,
            watch_until=_plus_minutes(now, output.watch_extend_minutes) if next_status == "watching" else None,
        )
        if output.answerability == "no_reply":
            self._mark_processing_processed(
                message=message,
                stage="task_session",
                task_id=task.id,
                attempt_count=outcome.attempt_count,
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
            resources=resources,
        )
        if not gate["allow"]:
            if gate["reason"] == "resource_needs_bot":
                action_id = self.approvals.notify_owner(task=task, reason="resource_needs_bot", payload={"message_id": message.message_id})
                self._mark_processing_processed(
                    message=message,
                    stage="task_session",
                    task_id=task.id,
                    attempt_count=outcome.attempt_count,
                )
                return ProcessingResult("owner_notification_created", task.id, action_id=action_id, reason=gate["reason"])
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
        return ProcessingResult("send_action_created", task.id, action_id=action_id, reason="gate_passed")

    def _reply_gate(
        self,
        *,
        task: TaskRecord,
        message: NormalizedMessage,
        output: TaskSessionOutput,
        composed: ComposedReply,
        resources: list[Any],
    ) -> dict[str, Any]:
        if output.answerability != "auto_reply":
            return {"allow": False, "reason": "needs_owner", "identity": "user"}
        if composed.had_forbidden_mentions:
            return {"allow": False, "reason": "forbidden_mentions", "identity": "user"}
        if not output.proposed_reply.strip() or not composed.text.strip():
            return {"allow": False, "reason": "empty_proposed_reply", "identity": "user"}
        chat_type = task.chat_type or message.chat_type
        policy = self._chat_policy(task.chat_id or message.chat_id)
        threshold = policy.confidence_threshold if chat_type == "group" else self.config.reply_policy.confidence_threshold
        risk_max = policy.risk_level_max if chat_type == "group" else self.config.reply_policy.risk_level_max
        if output.confidence < threshold:
            return {"allow": False, "reason": "low_confidence", "identity": "user"}
        if _risk_rank(output.risk_level) > _risk_rank(risk_max):
            return {"allow": False, "reason": "risk_too_high", "identity": "user"}
        resource_reason = _resource_gate_reason(resources, requires_resources=output.requires_resources)
        if resource_reason is not None:
            return {"allow": False, "reason": resource_reason, "identity": "user"}
        if chat_type == "p2p":
            if not self.config.reply_policy.p2p_auto_reply:
                return {"allow": False, "reason": "p2p_auto_reply_disabled", "identity": "user"}
            return {"allow": True, "reason": "ok", "identity": "user"}
        if chat_type == "group":
            if not message.direct_mention:
                return {"allow": False, "reason": "group_not_direct_mention", "identity": "user"}
            chat_configured = bool((task.chat_id or message.chat_id) in self.config.chats)
            if not chat_configured:
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
    ) -> list[str]:
        if session_id is not None:
            return [message.message_id]
        message_ids = self.store.list_task_message_ids(task.id)
        return message_ids or [message.message_id]

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
    ) -> int:
        action_id = self.approvals.notify_owner(
            task=None,
            reason="task_router_invalid_target",
            payload={"message_id": message.message_id, "target": target_task_id},
        )
        self._audit_router_ambiguity(
            message=message,
            reason="task_router_invalid_target",
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


def _router_prompt(*, message: NormalizedMessage, active: list[Any], historical: list[TaskRecord]) -> str:
    payload = {
        "instruction": "Route the incoming Feishu message. Return strict JSON only.",
        "schema": {
            "route": "new_task|attach_task|reopen_task|close_task|ignore|ambiguous",
            "target_task_id": "task short id or null",
            "confidence": "0..1",
            "reason": "short reason",
            "updated_watch_keys": [],
        },
        "message": _message_card(message),
        "active_candidates": [_candidate_card(candidate.task, candidate.matched_by) for candidate in active],
        "historical_candidates": [_task_card(task) for task in historical],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _task_session_prompt(
    *,
    task: TaskRecord,
    current_message_id: str,
    reply_target_message_ids: list[str],
    messages: list[Any],
    resources: list[Any],
) -> str:
    payload = {
        "instruction": "Handle this Feishu task. Return strict JSON only and do not include @ mentions.",
        "metadata": {
            "current_message_id": current_message_id,
            "root_message_id": task.root_message_id,
            "reply_target_message_ids": reply_target_message_ids,
        },
        "schema": {
            "task_label": "short label",
            "task_state": "needs_reply|watching|closed|waiting",
            "answerability": "auto_reply|needs_owner|no_reply",
            "confidence": "0..1",
            "proposed_reply": "reply without at-mentions",
            "reply_target_message_id": "current or root message id",
            "watch_action": "keep_watching|close",
            "watch_extend_minutes": 120,
            "risk_level": "low|medium|high",
            "safety_notes": [],
            "requires_resources": False,
        },
        "task": _task_card(task),
        "messages": [_row_message_card(row) for row in messages],
        "resources": [_resource_card(row) for row in resources],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _message_card(message: NormalizedMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "chat_id": message.chat_id,
        "chat_type": message.chat_type,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "sent_at": message.sent_at,
        "thread_id": message.thread_id,
        "reply_to_message_id": message.reply_to_message_id,
        "text": message.text,
        "direct_mention": message.direct_mention,
    }


def _row_message_card(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "chat_type": row["chat_type"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "sender_role": row["sender_role"],
        "sent_at": row["sent_at"],
        "text": row["text"],
        "thread_id": row["thread_id"],
        "reply_to_message_id": row["reply_to_message_id"],
    }


def _task_card(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": task.short_id,
        "status": task.status,
        "chat_id": task.chat_id,
        "chat_type": task.chat_type,
        "root_message_id": task.root_message_id,
        "task_label": task.task_label,
        "watch_until": task.watch_until,
    }


def _candidate_card(task: TaskRecord, matched_by: str) -> dict[str, Any]:
    return _task_card(task) | {"matched_by": matched_by}


def _resource_card(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "file_key": row["file_key"],
        "resource_type": row["resource_type"],
        "download_status": row["download_status"],
        "path": row["path"],
    }


def _risk_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(value, 2)


def _can_directly_approve(proposed_reply: str, composed: ComposedReply) -> bool:
    return bool(proposed_reply.strip()) and bool(composed.text.strip()) and not composed.had_forbidden_mentions


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


def _invalid_watch_keys(keys: list[str]) -> list[str]:
    return [key for key in keys if not isinstance(key, str) or WATCH_KEY_RE.fullmatch(key) is None]


def _resource_gate_reason(resources: list[Any], *, requires_resources: bool) -> str | None:
    if not requires_resources:
        return None
    statuses = {row["download_status"] for row in resources}
    if not statuses:
        return "resource_missing"
    if statuses <= {"downloaded"}:
        return None
    if statuses & {"bot_not_joined", "bot_invisible"}:
        return "resource_needs_bot"
    if statuses & {"failed", "missing_file"}:
        return "resource_unavailable"
    return "resource_unavailable"


def _hermes_result_error(result: HermesCliResult) -> str:
    parts = [
        result.error,
        result.stderr,
        result.stdout,
        f"exit_code={result.exit_code}" if result.exit_code is not None else None,
        "timed_out=True" if result.timed_out else None,
    ]
    return " ".join(str(part).strip() for part in parts if part).strip() or "Hermes call failed"


def _is_retryable_hermes_result(result: HermesCliResult) -> bool:
    error_text = _hermes_result_error(result).lower()
    if result.timed_out:
        return True
    if any(marker in error_text for marker in TERMINAL_HERMES_ERROR_MARKERS):
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
