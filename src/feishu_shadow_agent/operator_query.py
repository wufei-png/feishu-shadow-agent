from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .config import AppConfig
from .operator_queries.common import (
    OperatorQueryReadError,
    OperatorQueryUnavailable,
    _action_dto,
    _agent_audit_dto,
    _approval_dto,
    _attempt_dto,
    _coerce_limit,
    _coerce_offset,
    _dispatch_recommended_actions,
    _has_core_schema,
    _message_dto,
    _readback_summary,
    _ReadStoreUnavailable,
    _task_summary_dto,
)
from .operator_queries.health import (
    _RUN_RUNTIME_COLUMNS,
    HealthQuery,
    _daemon_liveness,
    _recent_errors,
    _run_runtime_summary,
    _store_read_uri,
)
from .operator_queries.message_detail import MessageDetailQuery
from .operator_queries.policy import PolicyQuery
from .store.sqlite_store import (
    RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    SQLiteStore,
)
from .types import ActionStatus, ApprovalStatus, TaskStatus, utc_now_iso

__all__ = [
    "OperatorQueryReadError",
    "OperatorQueryService",
    "OperatorQueryUnavailable",
]


class OperatorQueryService:
    """Read-only DTO boundary for operator status and detail views."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        policy_import_source: AppConfig | None = None,
        base_dir: Path | None = None,
        now: Callable[[], str] | None = None,
    ):
        self.store = store
        self.policy_import_source = policy_import_source
        self.base_dir = base_dir
        self._now = now or utc_now_iso
        self._policy_query = PolicyQuery(
            connect=self._connect,
            policy_import_source=policy_import_source,
        )
        self._health_query = HealthQuery(
            store=store,
            connect=self._connect,
            now=self._now,
            policy_status=self._policy_query.policy_status,
            validate_policy_store=self._policy_query.validate_policy_store,
        )
        self.policy_resolver = self._policy_query.policy_resolver

    def dashboard_snapshot(
        self,
        *,
        limit: int = 20,
        stale_after_seconds: int = 900,
        daemon_stale_after_seconds: int = RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    ) -> dict[str, Any]:
        now = self._now()
        try:
            with self._connect() as conn:
                last_run = conn.execute(
                    f"SELECT {_RUN_RUNTIME_COLUMNS} FROM runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                daemon_run = conn.execute(
                    f"""
                    SELECT {_RUN_RUNTIME_COLUMNS}
                    FROM runs
                    WHERE last_tick_started_at IS NOT NULL
                    ORDER BY last_tick_started_at DESC, started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
        except _ReadStoreUnavailable:
            last_run = None
            daemon_run = None
        failed_or_needs_review = self.list_dispatch_actions(
            statuses=(
                ActionStatus.FAILED.value,
                ActionStatus.FAILED_NEEDS_REVIEW.value,
            ),
            limit=limit,
        )
        failed_commands = self._failed_approval_commands(limit=limit)
        health_summary = self.health_issues(
            limit=limit,
            stale_after_seconds=stale_after_seconds,
            daemon_stale_after_seconds=daemon_stale_after_seconds,
        )["summary"]
        return {
            "daemon_liveness": _daemon_liveness(
                _run_runtime_summary(daemon_run) if daemon_run else None,
                now=now,
                stale_after_seconds=daemon_stale_after_seconds,
            ),
            "policy_status": self.policy_status(),
            "pending_approvals": self.list_approvals(
                status=ApprovalStatus.PENDING.value, limit=limit
            ),
            "active_tasks": self.list_tasks(
                status="watching", active_only=True, limit=limit
            ),
            "pending_actions": self.list_dispatch_actions(
                statuses=(ActionStatus.PENDING.value, ActionStatus.SENDING.value),
                limit=limit,
            ),
            "failed_or_needs_review_actions": failed_or_needs_review,
            "health_issue_summary": health_summary,
            "recent_health_warnings": self._recent_health_warnings(limit=limit),
            "recent_errors": _recent_errors(failed_commands, failed_or_needs_review),
            "last_run": _run_runtime_summary(last_run) if last_run else None,
            # Compatibility for current CLI users while status moves to the operator DTO boundary.
            "recent_expired_approvals": self.list_approvals(
                status=ApprovalStatus.EXPIRED.value, limit=limit
            ),
            "failed_approval_commands": failed_commands,
            "stale_sending_actions": self._stale_sending_actions(
                stale_after_seconds=stale_after_seconds,
                limit=limit,
            ),
            "recent_failed_actions": failed_or_needs_review,
        }

    def health_issues(
        self,
        *,
        limit: int = 20,
        stale_after_seconds: int = 900,
        daemon_stale_after_seconds: int = RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    ) -> dict[str, Any]:
        return self._health_query.health_issues(
            limit=limit,
            stale_after_seconds=stale_after_seconds,
            daemon_stale_after_seconds=daemon_stale_after_seconds,
        )

    def list_approvals(
        self,
        *,
        status: str | None = None,
        task_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if status is not None:
            where.append("a.status = ?")
            params.append(status)
        if task_id is not None:
            where.append("a.task_id = ?")
            params.append(task_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([_coerce_limit(limit), _coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT a.id, a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.status,
                           a.payload_json, a.preview, a.created_at, a.expires_at, a.resolved_at
                    FROM approvals a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    {where_sql}
                    ORDER BY a.created_at DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        now = self._now()
        return [_approval_dto(row, now=now) for row in rows]

    def approval_detail(self, approval_id: int | str) -> dict[str, Any] | None:
        where_sql, params = _id_lookup("a", approval_id)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT a.id, a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.status,
                           a.payload_json, a.preview, a.created_at, a.expires_at, a.resolved_at
                    FROM approvals a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE {where_sql}
                    """,
                    params,
                ).fetchone()
        except _ReadStoreUnavailable:
            return None
        if row is None:
            return None
        return _approval_dto(row, now=self._now(), include_payload=True)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        now = self._now()
        where = []
        params: list[Any] = []
        if status is not None:
            where.append("t.status = ?")
            params.append(status)
        if chat_id is not None:
            where.append("t.chat_id = ?")
            params.append(chat_id)
        if active_only:
            where.append("(t.watch_until IS NULL OR t.watch_until > ?)")
            params.append(now)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([_coerce_limit(limit), _coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT t.id, t.short_id, t.status, t.chat_id, t.chat_type, t.thread_id,
                           t.root_message_id, t.task_label, t.watch_until, t.updated_at,
                           t.agent_working_dir,
                           COUNT(tm.message_id) AS message_count,
                           (
                             SELECT COUNT(*)
                             FROM approvals ap
                             WHERE ap.task_id = t.id
                               AND ap.status = 'pending'
                           ) AS pending_approval_count,
                           (
                             SELECT COUNT(*)
                             FROM approvals ap
                             WHERE ap.task_id = t.id
                               AND ap.status = 'pending'
                               AND ap.expires_at IS NOT NULL
                               AND datetime(ap.expires_at) < datetime(?)
                           ) AS overdue_approval_count,
                           (
                             SELECT COUNT(*)
                             FROM actions ac
                             WHERE ac.task_id = t.id
                               AND ac.status = 'failed_needs_review'
                           ) AS failed_needs_review_action_count,
                           (
                             SELECT COUNT(*)
                             FROM actions ac
                             WHERE ac.task_id = t.id
                               AND ac.status = 'failed'
                           ) AS failed_action_count
                    FROM tasks t
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    {where_sql}
                    GROUP BY t.id
                    ORDER BY t.updated_at DESC, t.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [now, *params],
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_task_summary_dto(row) for row in rows]

    def task_detail(
        self, task_id: int | str, *, limit: int = 20
    ) -> dict[str, Any] | None:
        where_sql, params = _id_lookup("t", task_id)
        try:
            with self._connect() as conn:
                task = conn.execute(
                    f"""
                    SELECT t.*, COUNT(tm.message_id) AS message_count
                    FROM tasks t
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    WHERE {where_sql}
                    GROUP BY t.id
                    """,
                    params,
                ).fetchone()
                if task is None:
                    return None
                messages = conn.execute(
                    """
                    SELECT m.message_id, tm.role, m.sender_role, m.sent_at, m.text, tm.created_at
                    FROM task_messages tm
                    JOIN messages m ON m.message_id = tm.message_id
                    WHERE tm.task_id = ?
                    ORDER BY tm.created_at DESC, m.message_id DESC
                    LIMIT ?
                    """,
                    (int(task["id"]), _coerce_limit(limit)),
                ).fetchall()
                agent_audit_rows = conn.execute(
                    """
                    SELECT id, backend_provider, request_type, task_id, agent_session_id,
                           input_message_ids_json, input_resource_ids_json, response_json,
                           error, latency_ms, prompt_json, tool_permissions_profile, created_at
                    FROM agent_audits
                    WHERE task_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (int(task["id"]), _coerce_limit(limit)),
                ).fetchall()
        except _ReadStoreUnavailable:
            return None
        task_summary = _task_summary_dto(task)
        pending_approvals = self.list_approvals(
            status=ApprovalStatus.PENDING.value,
            task_id=int(task["id"]),
            limit=limit,
        )
        actions = self.list_dispatch_actions(task_id=int(task["id"]), limit=limit)
        return {
            **task_summary,
            "recent_messages": [_message_dto(row) for row in reversed(messages)],
            "pending_approvals": pending_approvals,
            "actions": actions,
            "agent_audits": [_agent_audit_dto(row) for row in agent_audit_rows],
            "effective_policy": self.effective_policy_summary(
                task["chat_id"], task["chat_type"]
            ),
            "recommended_actions": _task_recommended_actions(
                pending_approvals,
                actions,
                status=task_summary["status"],
                task_id=task_summary["task_id"],
            ),
        }

    def list_dispatch_actions(
        self,
        *,
        statuses: Iterable[str] | None = None,
        task_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if statuses is not None:
            status_values = tuple(statuses)
            if status_values:
                where.append(f"a.status IN ({','.join('?' for _ in status_values)})")
                params.extend(status_values)
        if task_id is not None:
            where.append("a.task_id = ?")
            params.append(task_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([_coerce_limit(limit), _coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    {where_sql}
                    ORDER BY a.updated_at DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_action_dto(row, include_payload=False) for row in rows]

    def dispatch_action_detail(self, action_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                action = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.id = ?
                    """,
                    (action_id,),
                ).fetchone()
                if action is None:
                    return None
                attempts = conn.execute(
                    """
                    SELECT *
                    FROM dispatch_attempts
                    WHERE action_id = ?
                    ORDER BY started_at, id
                    """,
                    (action_id,),
                ).fetchall()
        except _ReadStoreUnavailable:
            return None
        attempt_dtos = [_attempt_dto(row) for row in attempts]
        action_dto = _action_dto(action, include_payload=True)
        return {
            "action": action_dto,
            "attempts": attempt_dtos,
            "readback_summary": _readback_summary(attempt_dtos),
            "recommended_actions": _dispatch_recommended_actions(action_dto),
        }

    def message_detail(self, message_id: str) -> dict[str, Any] | None:
        try:
            return MessageDetailQuery(
                connect=self._connect,
                base_dir=self.base_dir,
                now=self._now,
            ).message_detail(message_id)
        except sqlite3.OperationalError as exc:
            raise OperatorQueryReadError(str(exc)) from exc

    def policy_status(self) -> dict[str, Any]:
        return self._policy_query.policy_status()

    def settings_runtime(self, config: AppConfig) -> dict[str, Any]:
        return self._policy_query.settings_runtime(config)

    def effective_policy_summary(
        self, chat_id: str | None, chat_type: str | None
    ) -> dict[str, Any]:
        return self._policy_query.effective_policy_summary(chat_id, chat_type)

    def policy_audit_history(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        scope: str | None = None,
        policy_key: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._policy_query.policy_audit_history(
            limit=limit,
            offset=offset,
            scope=scope,
            policy_key=policy_key,
            since=since,
        )

    def _failed_approval_commands(self, *, limit: int) -> list[dict[str, Any]]:
        return self._health_query._failed_approval_commands(limit=limit)

    def _recent_health_warnings(self, *, limit: int) -> list[dict[str, Any]]:
        return self._health_query._recent_health_warnings(limit=limit)

    def _stale_sending_actions(
        self, *, stale_after_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        return self._health_query._stale_sending_actions(
            stale_after_seconds=stale_after_seconds,
            limit=limit,
        )

    def _get_product_policy(self) -> dict[str, Any] | None:
        return self._policy_query._get_product_policy()

    def _get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        return self._policy_query._get_chat_product_policy(chat_id)

    def _list_chat_product_policies(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._policy_query._list_chat_product_policies(limit=limit)

    def _policy_import_diff(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return self._policy_query._policy_import_diff(conn)

    def _missing_store_policy_import_diff(self) -> dict[str, Any]:
        return self._policy_query._missing_store_policy_import_diff()

    def _policy_health_issue(
        self, policy_status: dict[str, Any], *, detected_at: str
    ) -> dict[str, Any] | None:
        return self._health_query.policy_health_issue(
            policy_status,
            detected_at=detected_at,
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.store.path.exists():
            raise _ReadStoreUnavailable("SQLite store does not exist.")
        uri = _store_read_uri(self.store.path)
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as exc:
            raise _ReadStoreUnavailable(str(exc)) from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        if not _has_core_schema(conn):
            conn.close()
            raise _ReadStoreUnavailable("SQLite store schema is not initialized.")
        return conn

    def _store_status(self) -> dict[str, Any]:
        return self._health_query._store_status()


def _task_recommended_actions(
    pending_approvals: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    status: str | None = None,
    task_id: str | None = None,
) -> list[str]:
    recommendations = []
    if task_id:
        if status == TaskStatus.WATCHING.value:
            recommendations.append(f"task close --task-id {task_id}")
        elif status in {
            TaskStatus.CLOSED.value,
            TaskStatus.CLOSED_BY_OWNER.value,
            TaskStatus.HUMAN_TAKEN_OVER.value,
        }:
            recommendations.append(f"task reopen --task-id {task_id}")
    if any(approval["is_overdue"] for approval in pending_approvals):
        recommendations.append("expire_overdue_approvals")
    elif pending_approvals:
        recommendations.append("review_pending_approvals")
    if any(
        action["status"] == ActionStatus.FAILED_NEEDS_REVIEW.value for action in actions
    ):
        recommendations.append("inspect_failed_needs_review_actions")
    elif any(action["status"] == ActionStatus.FAILED.value for action in actions):
        recommendations.append("retry_or_cancel_failed_actions")
    return recommendations


def _id_lookup(alias: str, value: int | str) -> tuple[str, list[Any]]:
    if isinstance(value, int):
        return f"{alias}.id = ?", [value]
    text = str(value)
    if text.isdigit():
        return f"{alias}.id = ?", [int(text)]
    return f"{alias}.short_id = ?", [text]
