from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .store.sqlite_store import SQLiteStore
from .types import ActionRecord, new_run_id


SUCCESS_STATUSES = {"applied", "no_change"}


class DispatchReadbackMarker(Protocol):
    def mark_action_sent_after_readback(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        ...


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

    def as_dict(self) -> dict[str, Any]:
        return {
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


class ApprovalCommandService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def approve(
        self,
        target_id: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self._apply("approve", target_id, actor=actor, reason=reason, command_id=command_id)

    def reject(
        self,
        target_id: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self._apply("reject", target_id, actor=actor, reason=reason, command_id=command_id)

    def send(
        self,
        task_id: str,
        final_reply: str,
        *,
        actor: str,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self._apply(
            "send",
            task_id,
            final_reply=final_reply,
            actor=actor,
            reason=reason,
            command_id=command_id,
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
    ) -> CommandResult:
        command_text = f"/{verb} {target_id}" if final_reply is None else f"/{verb} {target_id} {final_reply}"
        raw = self.store.apply_approval_command(
            message_id=command_id or new_run_id(f"operator_{verb}"),
            command=command_text,
            verb=verb,
            target_id=target_id,
            final_reply=final_reply,
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
    def __init__(self, store: SQLiteStore, *, readback_marker: DispatchReadbackMarker | None = None):
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
        status = "applied" if raw_status == "sent" else _dispatch_error_status(str(raw.get("error", "")))
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


class OperatorCommandService:
    def __init__(self, store: SQLiteStore, *, readback_marker: DispatchReadbackMarker | None = None):
        self.approvals = ApprovalCommandService(store)
        self.dispatch = DispatchCommandService(store, readback_marker=readback_marker)
        self.maintenance = MaintenanceCommandService(store)

    def approve(
        self,
        target_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self.approvals.approve(target_id, actor=actor, reason=reason, command_id=command_id)

    def reject(
        self,
        target_id: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self.approvals.reject(target_id, actor=actor, reason=reason, command_id=command_id)

    def send(
        self,
        task_id: str,
        final_reply: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
        command_id: str | None = None,
    ) -> CommandResult:
        return self.approvals.send(task_id, final_reply, actor=actor, reason=reason, command_id=command_id)

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
        return self.dispatch.mark_sent(action_id, sent_message_id=sent_message_id, actor=actor, reason=reason)

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

    def expire_approvals(self, *, actor: str = "operator", reason: str | None = None) -> CommandResult:
        return self.maintenance.expire_approvals(actor=actor, reason=reason)


def command_exit_code(result: CommandResult) -> int:
    return 0 if result.status in SUCCESS_STATUSES else 2


def _approval_command_status(raw_status: str, result: dict[str, Any]) -> str:
    if raw_status == "applied":
        return "applied"
    if raw_status == "duplicate":
        return "no_change"
    if result.get("pending_approval_ids") is not None or result.get("notification_action_id") is not None:
        return "conflict"
    return _approval_error_status(str(result.get("error", "")))


def _approval_error_status(error: str) -> str:
    lowered = error.lower()
    if "not found" in lowered:
        return "not_found"
    if "ambiguous" in lowered or "multiple pending" in lowered or "active send action already exists" in lowered:
        return "conflict"
    if "requires" in lowered or "unsupported command" in lowered or "missing" in lowered:
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
        "payload": action.payload,
        "result": action.result,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


def _dispatch_target(action_id: int) -> dict[str, Any]:
    return {"type": "dispatch_action", "action_id": action_id}


def _dispatch_next_actions(action_id: int | None) -> list[dict[str, Any]]:
    if action_id is None:
        return []
    return [{"command": "dispatch.inspect", "target": _dispatch_target(action_id)}]


def _mark_sent_warnings(raw: dict[str, Any]) -> list[str]:
    result = raw.get("result")
    if not isinstance(result, dict):
        return []
    warnings = result.get("warnings")
    return [str(warning) for warning in warnings] if isinstance(warnings, list) else []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
