from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..types import ActionStatus, ApprovalStatus
from .common import (
    _action_dto,
    _agent_audit_dto,
    _approval_dto,
    _attempt_dto,
    _coerce_limit,
    _loads_json_object,
    _readback_summary,
    _row_dict,
    _sqlite_like_contains,
    _task_summary_dto,
)


class MessageDetailQuery:
    """Read-only query slice for one Feishu message's processing context."""

    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        base_dir: Path | None = None,
        now: Callable[[], str],
    ):
        self._connect = connect
        self.base_dir = base_dir
        self._now = now

    def message_detail(self, message_id: str) -> dict[str, Any] | None:
        now = self._now()
        with self._connect() as conn:
            message = conn.execute(
                """
                SELECT message_id, chat_id, chat_type, sender_id, sender_name, sender_type,
                       sender_role, sent_at, thread_id, reply_to_message_id, direct_mention,
                       at_all, text, normalized_json, inserted_at
                FROM messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if message is None:
                return None
            routing_audits = conn.execute(
                """
                SELECT id, message_id, task_id, route, route_reason, candidates_count,
                       shortcut_hit, router_called, matched_by, target_task_id, created_at
                FROM routing_audits
                WHERE message_id = ?
                ORDER BY created_at, id
                """,
                (message_id,),
            ).fetchall()
            processing_rows = conn.execute(
                """
                SELECT id, message_id, task_id, stage, status, attempt_count,
                       last_error, terminal_reason, created_at, updated_at
                FROM message_processing
                WHERE message_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (message_id,),
            ).fetchall()
            resource_rows = conn.execute(
                """
                SELECT id, message_id, file_key, resource_type, download_status, path,
                       sha256, raw_json, created_at, updated_at
                FROM resources
                WHERE message_id = ?
                ORDER BY id
                """,
                (message_id,),
            ).fetchall()
            agent_audit_rows = conn.execute(
                """
                SELECT id, backend_provider, request_type, task_id, agent_session_id,
                       input_message_ids_json, input_resource_ids_json, response_json,
                       error, latency_ms, prompt_json, tool_permissions_profile, created_at
                FROM agent_audits
                WHERE input_message_ids_json LIKE ? ESCAPE '\\'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (
                    _sqlite_like_contains(json.dumps(message_id, ensure_ascii=False)),
                    _coerce_limit(50),
                ),
            ).fetchall()
            task_rows = conn.execute(
                """
                SELECT DISTINCT t.id, t.short_id, t.status, t.chat_id, t.chat_type, t.thread_id,
                       t.root_message_id, t.task_label, t.watch_until, t.updated_at,
                       t.agent_working_dir,
                       COUNT(tm_all.message_id) AS message_count
                FROM task_messages tm
                JOIN tasks t ON t.id = tm.task_id
                LEFT JOIN task_messages tm_all ON tm_all.task_id = t.id
                WHERE tm.message_id = ?
                GROUP BY t.id
                ORDER BY t.updated_at DESC, t.id DESC
                """,
                (message_id,),
            ).fetchall()
            task_ids = [int(row["id"]) for row in task_rows]
            approvals = _fetch_approvals_for_tasks(conn, task_ids)
            actions = _fetch_actions_for_message(
                conn, message_id=message_id, task_ids=task_ids
            )
            attempts_by_action = _fetch_attempts_for_actions(
                conn, [int(row["id"]) for row in actions]
            )

        approval_dtos = [
            _approval_dto(row, now=now, include_payload=False) for row in approvals
        ]
        action_dtos = [_action_dto(row, include_payload=False) for row in actions]
        recorded_dispatch_outcomes = [
            _recorded_dispatch_outcome(
                action, attempts_by_action.get(int(action["id"]), [])
            )
            for action in action_dtos
        ]
        return {
            "message": _message_detail_dto(message),
            "task_ids": task_ids,
            "task_summaries": [_task_summary_dto(row) for row in task_rows],
            "routing_audits": [_routing_audit_dto(row) for row in routing_audits],
            "processing": [_message_processing_dto(row) for row in processing_rows],
            "resources": [
                _resource_dto(row, base_dir=self.base_dir) for row in resource_rows
            ],
            "agent_audits": [
                audit
                for audit in (_agent_audit_dto(row) for row in agent_audit_rows)
                if message_id in audit["input_message_ids"]
            ],
            "approvals": approval_dtos,
            "actions": action_dtos,
            "recorded_dispatch_outcomes": recorded_dispatch_outcomes,
            "recommended_actions": _message_detail_recommended_actions(
                approval_dtos, action_dtos
            ),
        }


def _fetch_approvals_for_tasks(
    conn: sqlite3.Connection, task_ids: list[int]
) -> list[sqlite3.Row]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    return conn.execute(
        f"""
        SELECT a.id, a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.status,
               a.payload_json, a.preview, a.created_at, a.expires_at, a.resolved_at
        FROM approvals a
        LEFT JOIN tasks t ON t.id = a.task_id
        WHERE a.task_id IN ({placeholders})
        ORDER BY a.created_at DESC, a.id DESC
        """,
        task_ids,
    ).fetchall()


def _fetch_actions_for_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    task_ids: list[int],
) -> list[sqlite3.Row]:
    params: list[Any] = [message_id]
    task_filter = ""
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        task_filter = f" OR a.task_id IN ({placeholders})"
        params.extend(task_ids)
    return conn.execute(
        f"""
        SELECT a.*, t.short_id AS task_short_id
        FROM actions a
        LEFT JOIN tasks t ON t.id = a.task_id
        WHERE a.target_message_id = ?{task_filter}
        ORDER BY a.created_at, a.id
        """,
        params,
    ).fetchall()


def _fetch_attempts_for_actions(
    conn: sqlite3.Connection,
    action_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not action_ids:
        return {}
    placeholders = ",".join("?" for _ in action_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM dispatch_attempts
        WHERE action_id IN ({placeholders})
        ORDER BY action_id, started_at, id
        """,
        action_ids,
    ).fetchall()
    attempts: dict[int, list[dict[str, Any]]] = {
        action_id: [] for action_id in action_ids
    }
    for row in rows:
        attempts.setdefault(int(row["action_id"]), []).append(_attempt_dto(row))
    return attempts


def _message_detail_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "message_id": data["message_id"],
        "chat_id": data["chat_id"],
        "chat_type": data["chat_type"],
        "sender_id": data["sender_id"],
        "sender_name": data["sender_name"],
        "sender_type": data["sender_type"],
        "sender_role": data["sender_role"],
        "sent_at": data["sent_at"],
        "thread_id": data["thread_id"],
        "reply_to_message_id": data["reply_to_message_id"],
        "direct_mention": bool(data["direct_mention"]),
        "at_all": bool(data["at_all"]),
        "text": data["text"],
        "normalized": _loads_json_object(data["normalized_json"]),
        "inserted_at": data["inserted_at"],
    }


def _routing_audit_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data["id"],
        "message_id": data["message_id"],
        "task_id": data["task_id"],
        "route": data["route"],
        "route_reason": data["route_reason"],
        "candidates_count": data["candidates_count"],
        "shortcut_hit": bool(data["shortcut_hit"]),
        "router_called": bool(data["router_called"]),
        "matched_by": data["matched_by"],
        "target_task_id": data["target_task_id"],
        "created_at": data["created_at"],
    }


def _message_processing_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data["id"],
        "message_id": data["message_id"],
        "task_id": data["task_id"],
        "stage": data["stage"],
        "status": data["status"],
        "attempt_count": int(data.get("attempt_count") or 0),
        "last_error": data.get("last_error"),
        "terminal_reason": data.get("terminal_reason"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def _resource_dto(row: sqlite3.Row, *, base_dir: Path | None) -> dict[str, Any]:
    data = _row_dict(row)
    stored_path = data.get("path")
    path_exists = _resource_path_exists(stored_path, base_dir=base_dir)
    raw = _loads_json_object(data.get("raw_json"))
    sha256 = data.get("sha256")
    return {
        "id": data["id"],
        "message_id": data["message_id"],
        "file_key": data["file_key"],
        "resource_type": data["resource_type"],
        "download_status": data["download_status"],
        "path": stored_path,
        "path_exists": path_exists,
        "sha256": sha256,
        "sha256_short": None if not sha256 else str(sha256)[:12],
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "raw_summary": _resource_raw_summary(raw),
        "raw": raw,
    }


def _resource_path_exists(value: Any, *, base_dir: Path | None) -> bool | None:
    if base_dir is None or not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return (base_dir / path).is_file()


def _resource_raw_summary(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "reason",
        "error",
        "policy_source",
        "resource_dir_bytes",
        "max_resource_bytes",
        "max_resource_dir_bytes",
    )
    return {key: raw[key] for key in allowed if key in raw}


def _recorded_dispatch_outcome(
    action: dict[str, Any], attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "action_id": action["action_id"],
        "kind": action["kind"],
        "status": action["status"],
        "result_summary": action["result_summary"],
        "attempts": attempts,
        "readback_summary": _readback_summary(attempts),
    }


def _message_detail_recommended_actions(
    approvals: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if any(approval["is_overdue"] for approval in approvals):
        recommendations.append("expire_overdue_approvals")
    elif any(
        approval["status"] == ApprovalStatus.PENDING.value for approval in approvals
    ):
        recommendations.append("review_pending_approvals")
    if any(
        action["status"] == ActionStatus.FAILED_NEEDS_REVIEW.value for action in actions
    ):
        recommendations.append("inspect_failed_needs_review_actions")
    elif any(action["status"] == ActionStatus.FAILED.value for action in actions):
        recommendations.append("retry_or_cancel_failed_actions")
    return recommendations
