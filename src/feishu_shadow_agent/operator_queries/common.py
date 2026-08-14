from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, cast

from ..time_utils import parse_instant_or_none
from ..types import ActionStatus, ApprovalStatus, TaskStatus

_CORE_TABLES = frozenset(
    {
        "messages",
        "tasks",
        "task_messages",
        "approvals",
        "actions",
        "dispatch_attempts",
        "runs",
        "health_checks",
        "resources",
        "routing_audits",
        "agent_audits",
        "message_processing",
        "chat_policies",
        "product_policies",
        "policy_audits",
        "approval_commands",
    }
)


class OperatorQueryUnavailable(RuntimeError):
    pass


class OperatorQueryReadError(RuntimeError):
    pass


class _ReadStoreUnavailable(OperatorQueryUnavailable):
    pass


def _approval_dto(
    row: sqlite3.Row, *, now: str, include_payload: bool = False
) -> dict[str, Any]:
    data = _row_dict(row)
    short_id = str(data["short_id"])
    overdue_seconds = _approval_overdue_seconds(data.get("expires_at"), now=now)
    is_pending = data["status"] == ApprovalStatus.PENDING.value
    payload = _loads_json_object(data.get("payload_json"))
    postprocess = _loads_json_object(payload.get("postprocess"))
    dto: dict[str, Any] = {
        "id": data["id"],
        "approval_id": short_id,
        "short_id": short_id,
        "task_id": data["task_id"],
        "task_short_id": data["task_short_id"],
        "kind": data["kind"],
        "status": data["status"],
        "preview": data["preview"],
        "created_at": data["created_at"],
        "expires_at": data["expires_at"],
        "resolved_at": data.get("resolved_at"),
        "is_overdue": bool(is_pending and overdue_seconds > 0),
        "overdue_seconds": overdue_seconds if is_pending else 0,
        "recommended_action": _approval_recommended_action(
            data["status"], overdue_seconds
        ),
        "available_commands": _approval_available_commands(
            data["status"],
            short_id,
            data["task_short_id"],
            overdue_seconds=overdue_seconds,
            payload=payload,
        ),
        "postprocess_status": postprocess.get("status"),
        "postprocess_applied": postprocess.get("applied"),
        "postprocess_failure_reason": postprocess.get("failure_reason"),
    }
    if include_payload:
        dto["payload"] = payload
    return dto


def _approval_recommended_action(status: str, overdue_seconds: int) -> str:
    if status != ApprovalStatus.PENDING.value:
        return "none"
    return "expire" if overdue_seconds > 0 else "review"


def _approval_available_commands(
    status: str,
    approval_id: str,
    task_short_id: str | None,
    *,
    overdue_seconds: int,
    payload: dict[str, Any],
) -> list[str]:
    if status != ApprovalStatus.PENDING.value:
        return []
    if overdue_seconds > 0:
        return ["maintenance expire-approvals"]
    commands: list[str] = []
    if payload.get("approvable") is not False:
        commands.append(f"approve {approval_id}")
    commands.append(f"reject {approval_id}")
    if task_short_id:
        commands.append(f"send {task_short_id} <final_reply>")
    return commands


def _approval_overdue_seconds(expires_at: Any, *, now: str) -> int:
    expires_at_dt = _parse_datetime_or_none(expires_at)
    now_dt = _parse_datetime_or_none(now)
    if expires_at_dt is None or now_dt is None:
        return 0
    return max(0, int((now_dt - expires_at_dt).total_seconds()))


def _task_summary_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    short_id = str(data["short_id"])
    recommended_actions = _summary_recommended_actions(
        status=str(data["status"]),
        task_id=short_id,
        pending_approval_count=int(data.get("pending_approval_count") or 0),
        overdue_approval_count=int(data.get("overdue_approval_count") or 0),
        failed_needs_review_action_count=int(
            data.get("failed_needs_review_action_count") or 0
        ),
        failed_action_count=int(data.get("failed_action_count") or 0),
    )
    return {
        "id": data["id"],
        "task_id": short_id,
        "task_short_id": short_id,
        "short_id": short_id,
        "status": data["status"],
        "chat_id": data["chat_id"],
        "chat_type": data["chat_type"],
        "thread_id": data.get("thread_id"),
        "root_message_id": data.get("root_message_id"),
        "task_label": data.get("task_label"),
        "watch_until": data.get("watch_until"),
        "agent_working_dir": data.get("agent_working_dir"),
        "updated_at": data.get("updated_at"),
        "message_count": int(data.get("message_count") or 0),
        "recommended_actions": recommended_actions,
    }


def _message_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "message_id": data["message_id"],
        "role": data["role"],
        "sender_role": data["sender_role"],
        "sent_at": data["sent_at"],
        "text": data["text"],
        "created_at": data["created_at"],
    }


def _agent_audit_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    response = _loads_json_object(data.get("response_json"))
    prompt = _loads_json_object(data.get("prompt_json"))
    return {
        "id": data["id"],
        "backend_provider": data["backend_provider"],
        "request_type": data["request_type"],
        "task_id": data["task_id"],
        "agent_session_id": data["agent_session_id"],
        "input_message_ids": _loads_json_list(data.get("input_message_ids_json")),
        "input_resource_ids": _loads_json_list(data.get("input_resource_ids_json")),
        "response_summary": _agent_response_summary(response),
        "response": response,
        "error": data.get("error"),
        "latency_ms": data.get("latency_ms"),
        "tool_permissions_profile": data.get("tool_permissions_profile"),
        "prompt_debug": prompt,
        "created_at": data["created_at"],
    }


def _agent_response_summary(response: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in response.items():
        if isinstance(value, str):
            summary[key] = value if len(value) <= 120 else f"{value[:117]}..."
        elif isinstance(value, (bool, int, float)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = f"{len(cast(list[object], value))} item(s)"
        elif isinstance(value, dict):
            summary[key] = f"{len(cast(dict[object, object], value))} field(s)"
    return summary


def _action_dto(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    data = _row_dict(row)
    dto: dict[str, Any] = {
        "id": data["id"],
        "action_id": data["id"],
        "kind": data["kind"],
        "status": data["status"],
        "task_id": data["task_id"],
        "task_short_id": data.get("task_short_id"),
        "approval_id": data.get("approval_id"),
        "target_message_id": data["target_message_id"],
        "dry_run": bool(data["dry_run"]),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "result_summary": _result_summary(_loads_json_object(data.get("result_json"))),
    }
    dto["recommended_actions"] = _dispatch_recommended_actions(dto)
    if include_payload:
        dto["idempotency_key"] = data["idempotency_key"]
        dto["payload"] = _loads_json_object(data.get("payload_json"))
        dto["result"] = _loads_json_object(data.get("result_json"))
    return dto


def _attempt_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data["id"],
        "action_id": data["action_id"],
        "run_id": data["run_id"],
        "status": data["status"],
        "dry_run_result": _loads_json(data["dry_run_result_json"]),
        "send_result": _loads_json(data["send_result_json"]),
        "readback_result": _loads_json(data["readback_result_json"]),
        "sent_message_id": data["sent_message_id"],
        "error_stage": data["error_stage"],
        "started_at": data["started_at"],
        "finished_at": data["finished_at"],
    }


def _readback_summary(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    latest = attempts[-1] if attempts else None
    readback = latest.get("readback_result") if latest else None
    readback_data = cast(dict[str, Any], readback) if isinstance(readback, dict) else {}
    return {
        "attempt_count": len(attempts),
        "latest_status": None if latest is None else latest["status"],
        "sent_message_id": None if latest is None else latest["sent_message_id"],
        "error_stage": None if latest is None else latest["error_stage"],
        "readback_ok": readback_data.get("ok") is True,
        "readback_message_id": readback_data.get("message_id"),
    }


def _summary_recommended_actions(
    *,
    status: str,
    task_id: str,
    pending_approval_count: int,
    overdue_approval_count: int,
    failed_needs_review_action_count: int,
    failed_action_count: int,
) -> list[str]:
    recommendations: list[str] = []
    if status == TaskStatus.WATCHING.value:
        recommendations.append(f"task close --task-id {task_id}")
    elif status in {
        TaskStatus.CLOSED.value,
        TaskStatus.CLOSED_BY_OWNER.value,
        TaskStatus.HUMAN_TAKEN_OVER.value,
    }:
        recommendations.append(f"task reopen --task-id {task_id}")
    if overdue_approval_count > 0:
        recommendations.append("expire_overdue_approvals")
    elif pending_approval_count > 0:
        recommendations.append("review_pending_approvals")
    if failed_needs_review_action_count > 0:
        recommendations.append("inspect_failed_needs_review_actions")
    elif failed_action_count > 0:
        recommendations.append("retry_or_cancel_failed_actions")
    return recommendations


def _dispatch_recommended_actions(action: dict[str, Any]) -> list[str]:
    action_id = action["action_id"]
    if action["status"] == ActionStatus.FAILED_NEEDS_REVIEW.value:
        return [
            f"dispatch inspect --action-id {action_id}",
            f"dispatch mark-sent --action-id {action_id} --sent-message-id <message_id>",
            f"dispatch retry --action-id {action_id}",
            f"dispatch cancel --action-id {action_id}",
        ]
    if action["status"] == ActionStatus.FAILED.value:
        return [
            f"dispatch retry --action-id {action_id}",
            f"dispatch cancel --action-id {action_id}",
        ]
    return []


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not result:
        return {}
    return {
        key: result[key]
        for key in ("error_stage", "recovery_reason", "sent_message_id", "warnings")
        if key in result
    }


def _coerce_limit(limit: int) -> int:
    return max(1, min(int(limit), 100))


def _coerce_offset(offset: int) -> int:
    return max(0, int(offset))


def _sqlite_like_contains(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {key: row[key] for key in keys}


def json_row_dict(row: sqlite3.Row, *columns: str) -> dict[str, Any]:
    data = _row_dict(row)
    for column in columns:
        if column in data:
            data[column] = _loads_json(data[column])
    return data


def _loads_json(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _loads_json_object(value: Any) -> dict[str, Any]:
    loaded = _loads_json(value)
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


def _loads_json_list(value: Any) -> list[Any]:
    loaded = _loads_json(value)
    return cast(list[Any], loaded) if isinstance(loaded, list) else []


def _parse_datetime_or_none(value: Any) -> datetime | None:
    return parse_instant_or_none(value)


def _has_core_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ({})
        """.format(  # noqa: S608
            ",".join("?" for _ in _CORE_TABLES)
        ),
        tuple(sorted(_CORE_TABLES)),
    ).fetchall()
    table_names: set[str] = {str(row["name"]) for row in rows}
    return frozenset(table_names) == _CORE_TABLES


# Public read-only query helpers used by the operator DTO boundary.
ReadStoreUnavailable = _ReadStoreUnavailable
approval_dto = _approval_dto
task_summary_dto = _task_summary_dto
message_dto = _message_dto
agent_audit_dto = _agent_audit_dto
action_dto = _action_dto
attempt_dto = _attempt_dto
readback_summary = _readback_summary
dispatch_recommended_actions = _dispatch_recommended_actions
coerce_limit = _coerce_limit
coerce_offset = _coerce_offset
sqlite_like_contains = _sqlite_like_contains
has_core_schema = _has_core_schema
parse_datetime_or_none = _parse_datetime_or_none
row_dict = _row_dict
loads_json_object = _loads_json_object
