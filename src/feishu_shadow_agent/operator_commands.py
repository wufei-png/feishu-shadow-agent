from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .config import AppConfig, ChatPolicyConfig, ReplyPolicyConfig
from .operator_query import OperatorQueryService
from .store.sqlite_store import SQLiteStore
from .types import (
    ActionRecord,
    ApprovalOutcome,
    ExecutionMode,
    FeedbackReason,
    new_run_id,
)

SUCCESS_STATUSES = {"applied", "no_change"}
GLOBAL_POLICY_UPDATE_FIELDS = {
    "p2p_auto_reply",
    "unknown_group_auto_reply",
    "bot_joined",
    "reply_identity",
    "allow_user_fallback",
    "resource_download",
}
CHAT_POLICY_UPDATE_FIELDS = {
    "name",
    "auto_reply",
    "bot_joined",
    "reply_identity",
    "allow_user_fallback",
    "resource_download",
}


class DispatchReadbackMarker(Protocol):
    def mark_action_sent_after_readback(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        run_id: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CommandResult:
    status: str
    command: str
    actor: str
    target: dict[str, Any]
    changed: bool
    result: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "command": self.command,
            "actor": self.actor,
            "reason": self.reason,
            "target": self.target,
            "changed": self.changed,
            "result": self.result,
            "warnings": self.warnings,
            "next_actions": self.next_actions,
        }
        data.update(self.extra)
        return data


class ApprovalCommandService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        keep_watching_until_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self.keep_watching_until_factory = keep_watching_until_factory

    def approve(
        self,
        target_id: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self._apply(
            "approve",
            target_id,
            actor=actor,
            reason=reason,
            command_id=command_id,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
            requested_outcome="suggestion_sent",
        )

    def reject(
        self,
        target_id: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
        keep_watching: bool | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self._apply(
            "reject",
            target_id,
            actor=actor,
            reason=reason,
            command_id=command_id,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
            requested_outcome=(
                None
                if keep_watching is None
                else "no_send_keep_watching"
                if keep_watching
                else "no_send_end_task"
            ),
        )

    def send(
        self,
        task_id: str,
        final_reply: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self._apply(
            "send",
            task_id,
            final_reply=final_reply,
            actor=actor,
            reason=reason,
            command_id=command_id,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
            requested_outcome="edited_sent",
        )

    def apply_text(
        self,
        command: str,
        *,
        command_id: str,
        actor: str,
        execution_mode: ExecutionMode,
        keep_watching_until: str,
    ) -> CommandResult | None:
        normalized = command.strip()
        if not normalized.startswith("/"):
            return None
        match = re.match(r"^/(\S+)(?:\s+(\S+))?(?:\s+([\s\S]*))?$", normalized)
        if match is None:
            return None
        verb = match.group(1)
        target_id = match.group(2)
        final_reply = match.group(3)
        valid = (
            verb in {"approve", "reject"}
            and target_id is not None
            and final_reply is None
        ) or (verb == "send" and target_id is not None and final_reply is not None)
        return self._apply(
            verb if valid else "invalid",
            target_id if valid and target_id is not None else "",
            final_reply=final_reply if valid else None,
            actor=actor,
            reason=None,
            command_id=command_id,
            execution_mode=execution_mode,
            requested_outcome=(
                "suggestion_sent"
                if verb == "approve" and valid
                else "edited_sent"
                if verb == "send" and valid
                else None
            ),
            command_text=normalized,
            keep_watching_until=keep_watching_until,
        )

    def _apply(
        self,
        verb: str,
        target_id: str,
        *,
        actor: str,
        reason: str | None,
        command_id: str | None,
        final_reply: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
        requested_outcome: ApprovalOutcome | None = None,
        command_text: str | None = None,
        keep_watching_until: str | None = None,
    ) -> CommandResult:
        effective_command_text = command_text or (
            f"/{verb} {target_id}"
            if final_reply is None
            else f"/{verb} {target_id} {final_reply}"
        )
        raw = self.store.apply_approval_command(
            message_id=command_id or new_run_id(f"operator_{verb}"),
            command=effective_command_text,
            verb=verb,
            target_id=target_id,
            final_reply=final_reply,
            keep_watching_until=(
                keep_watching_until
                if keep_watching_until is not None
                else self.keep_watching_until_factory()
                if verb == "reject" and self.keep_watching_until_factory is not None
                else None
            ),
            actor=actor,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
            requested_outcome=requested_outcome,
        )
        raw_status = str(raw.get("status", "failed"))
        result = _dict_result(raw.get("result"))
        result["approval_command_status"] = raw_status
        status = _approval_command_status(raw_status, result)
        action_id = _int_or_none(result.get("action_id"))
        changed = status == "applied"
        return CommandResult(
            status=status,
            command=f"approval.{verb}",
            actor=actor,
            reason=reason,
            target={"type": "approval_or_task", "id": target_id},
            changed=changed,
            result=result,
            next_actions=_dispatch_next_actions(action_id),
        )


class DispatchCommandService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        readback_marker: DispatchReadbackMarker | None = None,
    ):
        self.store = store
        self.readback_marker = readback_marker

    def inspect(
        self,
        action_id: int,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        inspection = self.store.get_dispatch_inspection(action_id)
        if inspection is None:
            return _error_result(
                status="not_found",
                command="dispatch.inspect",
                actor=actor,
                reason=reason,
                target=_dispatch_target(action_id),
                error=f"action not found: {action_id}",
            )
        return CommandResult(
            status="no_change",
            command="dispatch.inspect",
            actor=actor,
            reason=reason,
            target=_dispatch_target(action_id),
            changed=False,
            result=inspection,
        )

    def mark_sent(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        actor: str,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> CommandResult:
        if self.readback_marker is None:
            return _error_result(
                status="validation_failed",
                command="dispatch.mark_sent",
                actor=actor,
                reason=reason,
                target=_dispatch_target(action_id),
                error="dispatch mark-sent requires a readback marker",
            )
        raw = self.readback_marker.mark_action_sent_after_readback(
            action_id,
            sent_message_id=sent_message_id,
            run_id=run_id or new_run_id("dispatch_recovery"),
        )
        raw_status = str(raw.get("status", "failed"))
        result = dict(raw)
        result["dispatch_command_status"] = raw_status
        status = (
            "applied"
            if raw_status == "sent"
            else _dispatch_error_status(str(raw.get("error", "")))
        )
        warnings = _mark_sent_warnings(raw)
        return CommandResult(
            status=status,
            command="dispatch.mark_sent",
            actor=actor,
            reason=reason,
            target=_dispatch_target(action_id),
            changed=status == "applied",
            result=result,
            warnings=warnings,
        )

    def retry(
        self,
        action_id: int,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        try:
            action = self.store.retry_dispatch_action(action_id)
        except ValueError as exc:
            return _error_result(
                status=_dispatch_error_status(str(exc)),
                command="dispatch.retry",
                actor=actor,
                reason=reason,
                target=_dispatch_target(action_id),
                error=str(exc),
            )
        return CommandResult(
            status="applied",
            command="dispatch.retry",
            actor=actor,
            reason=reason,
            target=_dispatch_target(action_id),
            changed=True,
            result={"action": _action_output(action)},
            next_actions=_dispatch_next_actions(action.id),
        )

    def cancel(
        self,
        action_id: int,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        before = self.store.get_action(action_id)
        try:
            action = self.store.cancel_dispatch_action(action_id)
        except ValueError as exc:
            return _error_result(
                status=_dispatch_error_status(str(exc)),
                command="dispatch.cancel",
                actor=actor,
                reason=reason,
                target=_dispatch_target(action_id),
                error=str(exc),
            )
        changed = before is None or before.status != action.status
        return CommandResult(
            status="applied" if changed else "no_change",
            command="dispatch.cancel",
            actor=actor,
            reason=reason,
            target=_dispatch_target(action_id),
            changed=changed,
            result={"action": _action_output(action)},
        )


class MaintenanceCommandService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def expire_approvals(
        self,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        expired = self.store.expire_pending_approvals()
        return CommandResult(
            status="applied" if expired else "no_change",
            command="maintenance.expire_approvals",
            actor=actor,
            reason=reason,
            target={"type": "approval_queue"},
            changed=expired > 0,
            result={"expired_approvals": expired},
        )


class TaskCommandService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def close(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        try:
            raw = self.store.close_task_by_operator(task_id)
        except KeyError as exc:
            return _error_result(
                status="not_found",
                command="task.close",
                actor=actor,
                reason=reason,
                target=_task_target(task_id),
                error=str(exc),
            )
        changed = bool(raw.get("changed"))
        return CommandResult(
            status="applied" if changed else "no_change",
            command="task.close",
            actor=actor,
            reason=reason,
            target=_task_target(task_id),
            changed=changed,
            result=raw,
            next_actions=_task_next_actions(raw.get("task")),
        )

    def reopen(
        self,
        task_id: str,
        *,
        watch_until: str,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        try:
            raw = self.store.reopen_task_by_operator(task_id, watch_until=watch_until)
        except KeyError as exc:
            return _error_result(
                status="not_found",
                command="task.reopen",
                actor=actor,
                reason=reason,
                target=_task_target(task_id),
                error=str(exc),
            )
        changed = bool(raw.get("changed"))
        return CommandResult(
            status="applied" if changed else "no_change",
            command="task.reopen",
            actor=actor,
            reason=reason,
            target=_task_target(task_id),
            changed=changed,
            result=raw,
            next_actions=_task_next_actions(raw.get("task")),
        )


class PolicyCommandService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def import_config(
        self,
        config: AppConfig,
        *,
        replace: bool = False,
        used_defaults: bool = False,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        raw = self.store.import_product_policy_from_config(
            config,
            replace=replace,
            used_defaults=used_defaults,
            actor=actor,
            reason=reason,
        )
        audit_count = int(raw.get("audit_count") or 0)
        diff = OperatorQueryService(
            self.store, policy_import_source=config
        ).policy_status()["policy_import_diff"]
        return CommandResult(
            status="applied" if audit_count > 0 else "no_change",
            command="policy.import_config",
            actor=actor,
            reason=reason,
            target={"type": "product_policy_store", "mode": raw.get("mode")},
            changed=audit_count > 0,
            result=raw,
            next_actions=[
                {"command": "status", "target": {"type": "operator_dashboard"}}
            ],
            extra={
                "audit_count": audit_count,
                "policy_import_diff": diff,
            },
        )

    def update_global_policy(
        self,
        changes: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        target = {"type": "global_policy", "key": "reply_policy"}
        try:
            normalized_changes = _policy_changes(
                changes, allowed_fields=GLOBAL_POLICY_UPDATE_FIELDS
            )
        except ValueError as exc:
            return _error_result(
                status="validation_failed",
                command="policy.update_global",
                actor=actor,
                reason=reason,
                target=target,
                error=str(exc),
            )
        if not normalized_changes:
            return _error_result(
                status="validation_failed",
                command="policy.update_global",
                actor=actor,
                reason=reason,
                target=target,
                error="at least one policy field is required",
            )
        old_policy = self.store.get_product_policy()
        if old_policy is None:
            return _error_result(
                status="not_found",
                command="policy.update_global",
                actor=actor,
                reason=reason,
                target=target,
                error="global Product Policy is not initialized; run `policy import-config` first",
            )
        try:
            new_policy = _merged_global_policy(old_policy, normalized_changes)
        except (TypeError, ValidationError, ValueError) as exc:
            return _error_result(
                status="validation_failed",
                command="policy.update_global",
                actor=actor,
                reason=reason,
                target=target,
                error=str(exc),
            )
        raw = self.store.update_product_policy(
            new_policy,
            actor=actor,
            reason=reason,
        )
        return _policy_mutation_result(
            command="policy.update_global",
            actor=actor,
            reason=reason,
            target=target,
            raw=raw,
        )

    def update_chat_policy(
        self,
        chat_id: str,
        changes: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        normalized_chat_id = chat_id.strip()
        target = {"type": "chat_policy", "chat_id": normalized_chat_id}
        try:
            normalized_changes = _policy_changes(
                changes, allowed_fields=CHAT_POLICY_UPDATE_FIELDS
            )
        except ValueError as exc:
            return _error_result(
                status="validation_failed",
                command="policy.update_chat",
                actor=actor,
                reason=reason,
                target=target,
                error=str(exc),
            )
        if not normalized_chat_id:
            return _error_result(
                status="validation_failed",
                command="policy.update_chat",
                actor=actor,
                reason=reason,
                target=target,
                error="chat_id is required",
            )
        if not normalized_changes:
            return _error_result(
                status="validation_failed",
                command="policy.update_chat",
                actor=actor,
                reason=reason,
                target=target,
                error="at least one policy field is required",
            )
        if self.store.get_product_policy() is None:
            return _error_result(
                status="not_found",
                command="policy.update_chat",
                actor=actor,
                reason=reason,
                target=target,
                error="global Product Policy is not initialized; run `policy import-config` first",
            )
        old_policy = self.store.get_chat_product_policy(normalized_chat_id)
        base_policy = old_policy or {
            "chat_id": normalized_chat_id,
            **ChatPolicyConfig().model_dump(mode="json"),
        }
        try:
            new_policy = _merged_chat_policy(base_policy, normalized_changes)
        except (TypeError, ValidationError, ValueError) as exc:
            return _error_result(
                status="validation_failed",
                command="policy.update_chat",
                actor=actor,
                reason=reason,
                target=target,
                error=str(exc),
            )
        raw = self.store.upsert_chat_product_policy(
            new_policy,
            actor=actor,
            reason=reason,
        )
        return _policy_mutation_result(
            command="policy.update_chat",
            actor=actor,
            reason=reason,
            target=target,
            raw=raw,
        )

    def delete_chat_policy(
        self,
        chat_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> CommandResult:
        normalized_chat_id = chat_id.strip()
        target = {"type": "chat_policy", "chat_id": normalized_chat_id}
        if not normalized_chat_id:
            return _error_result(
                status="validation_failed",
                command="policy.delete_chat",
                actor=actor,
                reason=reason,
                target=target,
                error="chat_id is required",
            )
        raw = self.store.delete_chat_product_policy(
            normalized_chat_id,
            actor=actor,
            reason=reason,
        )
        if not raw.get("changed"):
            return _error_result(
                status="not_found",
                command="policy.delete_chat",
                actor=actor,
                reason=reason,
                target=target,
                error=f"chat policy not found: {normalized_chat_id}",
            )
        return _policy_mutation_result(
            command="policy.delete_chat",
            actor=actor,
            reason=reason,
            target=target,
            raw=raw,
        )


class OperatorCommandService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        readback_marker: DispatchReadbackMarker | None = None,
        keep_watching_until_factory: Callable[[], str] | None = None,
    ):
        self.approvals = ApprovalCommandService(
            store, keep_watching_until_factory=keep_watching_until_factory
        )
        self.dispatch = DispatchCommandService(store, readback_marker=readback_marker)
        self.maintenance = MaintenanceCommandService(store)
        self.tasks = TaskCommandService(store)
        self.policy = PolicyCommandService(store)

    def approve(
        self,
        target_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self.approvals.approve(
            target_id,
            actor=actor,
            reason=reason,
            command_id=command_id,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
        )

    def apply_approval_text(
        self,
        command: str,
        *,
        command_id: str,
        actor: str,
        execution_mode: ExecutionMode,
        keep_watching_until: str,
    ) -> CommandResult | None:
        return self.approvals.apply_text(
            command,
            command_id=command_id,
            actor=actor,
            execution_mode=execution_mode,
            keep_watching_until=keep_watching_until,
        )

    def reject(
        self,
        target_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
        keep_watching: bool | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self.approvals.reject(
            target_id,
            actor=actor,
            reason=reason,
            command_id=command_id,
            keep_watching=keep_watching,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
        )

    def send(
        self,
        task_id: str,
        final_reply: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self.approvals.send(
            task_id,
            final_reply,
            actor=actor,
            reason=reason,
            command_id=command_id,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
        )

    def do_not_send(
        self,
        approval_id: str,
        *,
        keep_watching: bool,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> CommandResult:
        return self.reject(
            approval_id,
            actor=actor,
            reason=reason,
            command_id=command_id,
            keep_watching=keep_watching,
            feedback_reason=feedback_reason,
            note=note,
            execution_mode=execution_mode,
        )

    def inspect_dispatch_action(
        self,
        action_id: int,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.dispatch.inspect(action_id, actor=actor, reason=reason)

    def mark_dispatch_sent(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.dispatch.mark_sent(
            action_id, sent_message_id=sent_message_id, actor=actor, reason=reason
        )

    def retry_dispatch_action(
        self,
        action_id: int,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.dispatch.retry(action_id, actor=actor, reason=reason)

    def cancel_dispatch_action(
        self,
        action_id: int,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.dispatch.cancel(action_id, actor=actor, reason=reason)

    def expire_approvals(
        self, *, actor: str = "operator", reason: str | None = None
    ) -> CommandResult:
        return self.maintenance.expire_approvals(actor=actor, reason=reason)

    def close_task(
        self,
        task_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.tasks.close(task_id, actor=actor, reason=reason)

    def reopen_task(
        self,
        task_id: str,
        *,
        watch_until: str,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.tasks.reopen(
            task_id, watch_until=watch_until, actor=actor, reason=reason
        )

    def import_policy_config(
        self,
        config: AppConfig,
        *,
        replace: bool = False,
        used_defaults: bool = False,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.policy.import_config(
            config,
            replace=replace,
            used_defaults=used_defaults,
            actor=actor,
            reason=reason,
        )

    def update_global_policy(
        self,
        changes: dict[str, Any],
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.policy.update_global_policy(
            changes,
            actor=actor,
            reason=reason,
        )

    def update_chat_policy(
        self,
        chat_id: str,
        changes: dict[str, Any],
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.policy.update_chat_policy(
            chat_id,
            changes,
            actor=actor,
            reason=reason,
        )

    def delete_chat_policy(
        self,
        chat_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> CommandResult:
        return self.policy.delete_chat_policy(
            chat_id,
            actor=actor,
            reason=reason,
        )


def command_exit_code(result: CommandResult) -> int:
    return 0 if result.status in SUCCESS_STATUSES else 2


def _policy_changes(
    changes: dict[str, Any], *, allowed_fields: set[str]
) -> dict[str, Any]:
    normalized = {key: value for key, value in changes.items() if value is not None}
    unsupported = sorted(set(normalized) - allowed_fields)
    if unsupported:
        raise ValueError(f"unsupported policy field(s): {', '.join(unsupported)}")
    return normalized


def _merged_global_policy(
    old_policy: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    new_policy = copy.deepcopy(old_policy)
    reply_policy = dict(new_policy.get("reply_policy") or {})
    default_chat_policy = dict(new_policy.get("default_chat_policy") or {})
    for key, value in changes.items():
        if key in {"p2p_auto_reply", "unknown_group_auto_reply"}:
            reply_policy[key] = value
        else:
            default_chat_policy[key] = value
    reply_policy = ReplyPolicyConfig.model_validate(reply_policy).model_dump(
        mode="json"
    )
    validated_default = ChatPolicyConfig.model_validate(
        {
            "name": "",
            "auto_reply": False,
            **default_chat_policy,
        }
    ).model_dump(mode="json")
    return {
        "reply_policy": reply_policy,
        "default_chat_policy": {
            key: validated_default[key]
            for key in (
                "bot_joined",
                "reply_identity",
                "allow_user_fallback",
                "resource_download",
            )
        },
    }


def _merged_chat_policy(
    old_policy: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    new_policy = {**old_policy, **changes}
    chat_id = str(new_policy.get("chat_id", "")).strip()
    if not chat_id:
        raise ValueError("chat_id is required")
    validated = ChatPolicyConfig.model_validate(
        {key: value for key, value in new_policy.items() if key != "chat_id"}
    ).model_dump(mode="json")
    return {"chat_id": chat_id, **validated}


def _policy_mutation_result(
    *,
    command: str,
    actor: str,
    reason: str | None,
    target: dict[str, Any],
    raw: dict[str, Any],
) -> CommandResult:
    changed = bool(raw.get("changed"))
    audit_id = raw.get("audit_id")
    return CommandResult(
        status="applied" if changed else "no_change",
        command=command,
        actor=actor,
        reason=reason,
        target=target,
        changed=changed,
        result=raw,
        next_actions=[{"command": "status", "target": {"type": "operator_dashboard"}}],
        extra={
            "old_policy": raw.get("old_policy"),
            "new_policy": raw.get("new_policy"),
            "audit_id": audit_id,
            "audit_count": 1 if audit_id is not None else 0,
        },
    )


def _approval_command_status(raw_status: str, result: dict[str, Any]) -> str:
    if raw_status == "applied":
        return "applied"
    if raw_status == "duplicate":
        return "no_change"
    if (
        result.get("pending_approval_ids") is not None
        or result.get("notification_action_id") is not None
    ):
        return "conflict"
    return _approval_error_status(str(result.get("error", "")))


def _approval_error_status(error: str) -> str:
    lowered = error.lower()
    if "not found" in lowered:
        return "not_found"
    if (
        "ambiguous" in lowered
        or "multiple pending" in lowered
        or "active send action already exists" in lowered
    ):
        return "conflict"
    if (
        "requires" in lowered
        or "unsupported command" in lowered
        or "missing" in lowered
    ):
        return "validation_failed"
    if "not watching" in lowered:
        return "conflict"
    return "failed"


def _dispatch_error_status(error: str) -> str:
    lowered = error.lower()
    if "not found" in lowered:
        return "not_found"
    if (
        "active send action already exists" in lowered
        or "cannot be marked sent" in lowered
        or "cannot be cancelled" in lowered
    ):
        return "conflict"
    if (
        "only accepts" in lowered
        or "is required" in lowered
        or "readback evidence" in lowered
        or "reply_to_message_id" in lowered
        or "mentions" in lowered
        or "text mismatch" in lowered
        or "text did not match" in lowered
    ):
        return "validation_failed"
    return "failed"


def _error_result(
    *,
    status: str,
    command: str,
    actor: str,
    reason: str | None,
    target: dict[str, Any],
    error: str,
) -> CommandResult:
    return CommandResult(
        status=status,
        command=command,
        actor=actor,
        reason=reason,
        target=target,
        changed=False,
        result={"error": error},
    )


def _dict_result(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _action_output(action: ActionRecord) -> dict[str, Any]:
    return {
        "id": action.id,
        "idempotency_key": action.idempotency_key,
        "task_id": action.task_id,
        "approval_id": action.approval_id,
        "kind": action.kind,
        "status": action.status,
        "target_message_id": action.target_message_id,
        "dry_run": action.dry_run,
        "execution_mode": action.execution_mode,
        "payload": action.payload,
        "result": action.result,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


def _dispatch_target(action_id: int) -> dict[str, Any]:
    return {"type": "dispatch_action", "action_id": action_id}


def _task_target(task_id: str) -> dict[str, Any]:
    return {"type": "task", "id": task_id}


def _dispatch_next_actions(action_id: int | None) -> list[dict[str, Any]]:
    if action_id is None:
        return []
    return [{"command": "dispatch.inspect", "target": _dispatch_target(action_id)}]


def _task_next_actions(task: Any) -> list[dict[str, Any]]:
    if not isinstance(task, dict):
        return []
    task_id = task.get("task_id")
    if not task_id:
        return []
    return [{"command": "status", "target": {"type": "task", "id": task_id}}]


def _mark_sent_warnings(raw: dict[str, Any]) -> list[str]:
    result = raw.get("result")
    if not isinstance(result, dict):
        return []
    warnings = result.get("warnings")
    return [str(warning) for warning in warnings] if isinstance(warnings, list) else []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
