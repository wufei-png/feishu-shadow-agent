from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig, ChatPolicyConfig
from .operator_queries.common import (
    _action_dto,
    _agent_audit_dto,
    _approval_dto,
    _attempt_dto,
    _coerce_limit,
    _coerce_offset,
    _dispatch_recommended_actions,
    _json_row_dict,
    _loads_json_object,
    _message_dto,
    _parse_datetime_or_none,
    _readback_summary,
    _row_dict,
    _task_summary_dto,
)
from .operator_queries.message_detail import MessageDetailQuery
from .policy import PolicyResolver, ProductPolicyInvalidError, ProductPolicyMissingError
from .settings_catalog import CONFIG_VALUE_PATHS
from .store.sqlite_store import (
    LATEST_NON_OK_HEALTH_CHECKS_SQL,
    PRODUCT_POLICY_KEY,
    RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    SQLiteStore,
)
from .types import ActionStatus, ApprovalStatus, TaskStatus, utc_now_iso

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

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[^\s'\"`]+)")
_SEVERITY_ORDER = {
    "info": 0,
    "warning": 1,
    "error": 2,
    "critical": 3,
}
_RUN_RUNTIME_COLUMNS = """
run_id, started_at, finished_at, status, dry_run, last_heartbeat_at,
last_tick_started_at, last_tick_finished_at, last_tick_status
"""


class OperatorQueryUnavailable(RuntimeError):
    pass


class OperatorQueryReadError(RuntimeError):
    pass


class _ReadStoreUnavailable(OperatorQueryUnavailable):
    pass


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
        self.policy_resolver = PolicyResolver(_ReadOnlyProductPolicyRepository(self))

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
        now = self._now()
        store_status = self._store_status()
        issues: list[dict[str, Any]] = []
        runtime: dict[str, Any] = {
            "store": store_status,
            "daemon_liveness": {},
            "last_run": None,
        }
        recent_failed_commands: list[dict[str, Any]] = []
        recent_failed_dispatch_actions: list[dict[str, Any]] = []

        if store_status["status"] != "available":
            issues.append(
                _health_issue(
                    issue_id=f"store-{store_status['status']}",
                    severity="critical",
                    category="store",
                    title=_store_issue_title(store_status["status"]),
                    detail=_store_issue_detail(store_status["status"]),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

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
                failed_commands = conn.execute(
                    """
                    SELECT message_id, command, status, result_json, updated_at
                    FROM approval_commands
                    WHERE status != 'applied' AND status != 'duplicate'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (_coerce_limit(limit),),
                ).fetchall()
                health_rows = conn.execute(
                    LATEST_NON_OK_HEALTH_CHECKS_SQL,
                    (_coerce_limit(limit),),
                ).fetchall()
                failed_dispatch_rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status IN ('failed', 'failed_needs_review')
                    ORDER BY a.updated_at DESC, a.id DESC
                    """
                ).fetchall()
                stale_rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status = 'sending'
                      AND datetime(a.updated_at) <= datetime(?)
                    ORDER BY a.updated_at, a.id
                    """,
                    (_minus_seconds(now, stale_after_seconds),),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            issues.append(
                _health_issue(
                    issue_id="store-read-error",
                    severity="critical",
                    category="store",
                    title="Store read failed",
                    detail=_safe_detail(
                        str(exc), fallback="The operator store could not be read."
                    ),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

        try:
            last_run_dto = _run_runtime_summary(last_run) if last_run else None
            daemon_run_dto = _run_runtime_summary(daemon_run) if daemon_run else None
            daemon_liveness = _daemon_liveness(
                daemon_run_dto,
                now=now,
                stale_after_seconds=daemon_stale_after_seconds,
            )
            policy_status = self.policy_status()
            failed_dispatch_actions = [
                _action_dto(row, include_payload=False) for row in failed_dispatch_rows
            ]
            stale_sending_actions = [
                _action_dto(row, include_payload=False) for row in stale_rows
            ]
            recent_failed_commands = [
                _approval_command_summary(row) for row in failed_commands
            ]
            recent_failed_dispatch_actions = failed_dispatch_actions[
                : _coerce_limit(limit)
            ]

            runtime |= {
                "daemon_liveness": daemon_liveness,
                "last_run": last_run_dto,
                "policy_status": policy_status,
            }

            daemon_issue = _daemon_health_issue(daemon_liveness, detected_at=now)
            if daemon_issue is not None:
                issues.append(daemon_issue)
            policy_issue = self._policy_health_issue(policy_status, detected_at=now)
            if policy_issue is not None:
                issues.append(policy_issue)
        except sqlite3.DatabaseError:
            runtime["store"] = {
                "status": "schema_incompatible",
                "available": False,
                "schema_initialized": False,
            }
            issues.append(
                _health_issue(
                    issue_id="store-schema_incompatible",
                    severity="critical",
                    category="store",
                    title=_store_issue_title("schema_incompatible"),
                    detail=_store_issue_detail("schema_incompatible"),
                    detected_at=now,
                    links=[{"type": "settings", "id": "storage"}],
                    recommended_actions=["inspect_settings", "run_doctor"],
                )
            )
            return _health_response(
                generated_at=now,
                runtime=runtime,
                issues=issues,
                recent_failed_commands=recent_failed_commands,
                recent_failed_dispatch_actions=recent_failed_dispatch_actions,
            )

        for row in health_rows:
            issue = _health_check_issue(row)
            if issue is not None:
                issues.append(issue)
        for action in failed_dispatch_actions:
            issues.append(_dispatch_action_issue(action, detected_at=now))
        for action in stale_sending_actions:
            issues.append(_stale_sending_issue(action, detected_at=now))

        issues.sort(
            key=lambda issue: (
                _SEVERITY_ORDER.get(issue["severity"], 0),
                str(issue["detected_at"]),
            ),
            reverse=True,
        )
        open_issue_count = len(issues)
        return _health_response(
            generated_at=now,
            runtime=runtime,
            issues=issues[: _coerce_limit(limit)],
            open_issue_count=open_issue_count,
            recent_failed_commands=recent_failed_commands,
            recent_failed_dispatch_actions=recent_failed_dispatch_actions,
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
        try:
            with self._connect() as conn:
                global_policy = conn.execute(
                    "SELECT policy_json, updated_at FROM product_policies WHERE key = ?",
                    (PRODUCT_POLICY_KEY,),
                ).fetchone()
                chat_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM chat_policies"
                ).fetchone()
                diff = self._policy_import_diff(conn)
        except _ReadStoreUnavailable:
            return {
                "initialized": False,
                "global_policy_updated_at": None,
                "chat_policy_count": 0,
                "policy_import_diff": self._missing_store_policy_import_diff(),
            }
        return {
            "initialized": global_policy is not None,
            "global_policy_updated_at": None
            if global_policy is None
            else global_policy["updated_at"],
            "chat_policy_count": 0 if chat_count is None else int(chat_count["count"]),
            "policy_import_diff": diff,
        }

    def settings_runtime(self, config: AppConfig) -> dict[str, Any]:
        policy_status = self.policy_status()
        global_policy = self._get_product_policy()
        chat_policies = self._list_chat_product_policies()
        return {
            "values": _settings_values(
                config, policy_status=policy_status, global_policy=global_policy
            ),
            "global_policy": global_policy,
            "chat_policies": chat_policies,
            "policy_status": policy_status,
            "policy_audit_history": self.policy_audit_history(limit=20),
        }

    def effective_policy_summary(
        self, chat_id: str | None, chat_type: str | None
    ) -> dict[str, Any]:
        if not chat_id:
            return _empty_effective_policy("unknown_chat")
        try:
            policy = self.policy_resolver.resolve_chat_policy(chat_id, chat_type)
        except ProductPolicyMissingError as exc:
            return _empty_effective_policy("uninitialized", error=str(exc))
        except ProductPolicyInvalidError as exc:
            return _empty_effective_policy("invalid", error=str(exc))
        return {
            "policy_source": policy.policy_source,
            "auto_reply": policy.auto_reply,
            "bot_joined": policy.bot_joined,
            "reply_identity": policy.reply_identity,
            "allow_user_fallback": policy.allow_user_fallback,
            "resource_download": policy.resource_download,
        }

    def policy_audit_history(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        scope: str | None = None,
        policy_key: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        if policy_key is not None:
            where.append("policy_key = ?")
            params.append(policy_key)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([_coerce_limit(limit), _coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, scope, policy_key, actor, reason, created_at, old_json, new_json
                    FROM policy_audits
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_policy_audit_dto(row) for row in rows]

    def _failed_approval_commands(self, *, limit: int) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT message_id, command, status, result_json, updated_at
                    FROM approval_commands
                    WHERE status != 'applied' AND status != 'duplicate'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (_coerce_limit(limit),),
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_approval_command_summary(row) for row in rows]

    def _recent_health_warnings(self, *, limit: int) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    LATEST_NON_OK_HEALTH_CHECKS_SQL,
                    (_coerce_limit(limit),),
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_health_warning_dto(row) for row in rows]

    def _stale_sending_actions(
        self, *, stale_after_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        cutoff = _minus_seconds(self._now(), stale_after_seconds)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.status = 'sending'
                      AND datetime(a.updated_at) <= datetime(?)
                    ORDER BY a.updated_at, a.id
                    LIMIT ?
                    """,
                    (cutoff, _coerce_limit(limit)),
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_action_dto(row, include_payload=False) for row in rows]

    def _get_product_policy(self) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT policy_json FROM product_policies WHERE key = ?",
                    (PRODUCT_POLICY_KEY,),
                ).fetchone()
        except _ReadStoreUnavailable:
            return None
        return None if row is None else _loads_json_object(row["policy_json"])

    def _get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download
                    FROM chat_policies
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
        except _ReadStoreUnavailable:
            return None
        return None if row is None else _chat_policy_from_row(row)

    def _list_chat_product_policies(self, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download, updated_at
                    FROM chat_policies
                    ORDER BY chat_id
                    LIMIT ?
                    """,
                    (_coerce_limit(limit),),
                ).fetchall()
        except _ReadStoreUnavailable:
            return []
        return [_chat_policy_runtime_dto(row) for row in rows]

    def _policy_import_diff(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if self.policy_import_source is None:
            return {
                "status": "unknown",
                "message": "No Policy Import Source was provided for comparison.",
            }
        source_global = _global_policy_from_import_source(self.policy_import_source)
        global_row = conn.execute(
            "SELECT policy_json FROM product_policies WHERE key = ?",
            (PRODUCT_POLICY_KEY,),
        ).fetchone()
        missing_global = global_row is None
        changed_global = False
        if global_row is not None:
            changed_global = (
                _loads_json_object(global_row["policy_json"]) != source_global
            )

        missing_chats: list[str] = []
        changed_chats: list[str] = []
        for chat_id, chat_config in sorted(self.policy_import_source.chats.items()):
            source_chat = _chat_policy_from_import_source(chat_id, chat_config)
            row = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                missing_chats.append(chat_id)
            elif _chat_policy_from_row(row) != source_chat:
                changed_chats.append(chat_id)

        if (
            not missing_global
            and not changed_global
            and not missing_chats
            and not changed_chats
        ):
            return {
                "status": "matches",
                "message": "Policy Import Source matches Product Policy Store for global policy and config-listed chats.",
                "missing_global": False,
                "changed_global": False,
                "missing_chats": [],
                "changed_chats": [],
            }
        return {
            "status": "differs",
            "message": (
                "Policy Import Source differs from Product Policy Store; import-config would insert missing "
                "rows and import-config --replace would update changed rows."
            ),
            "missing_global": missing_global,
            "changed_global": changed_global,
            "missing_chats": missing_chats,
            "changed_chats": changed_chats,
        }

    def _missing_store_policy_import_diff(self) -> dict[str, Any]:
        if self.policy_import_source is None:
            return {
                "status": "unknown",
                "message": "No Policy Import Source was provided for comparison.",
            }
        return {
            "status": "differs",
            "message": (
                "Policy Import Source differs from Product Policy Store because no initialized store "
                "is available to compare."
            ),
            "missing_global": True,
            "changed_global": False,
            "missing_chats": sorted(self.policy_import_source.chats),
            "changed_chats": [],
        }

    def _policy_health_issue(
        self, policy_status: dict[str, Any], *, detected_at: str
    ) -> dict[str, Any] | None:
        if policy_status.get("initialized") is not True:
            return _health_issue(
                issue_id="policy-uninitialized",
                severity="critical",
                category="policy",
                title="Product Policy Store is not initialized",
                detail="Daemon runtime will fail closed until global Product Policy is imported.",
                detected_at=detected_at,
                links=[{"type": "policy", "id": "global"}],
                recommended_actions=["import_config"],
            )
        try:
            self.policy_resolver.resolve_chat_policy(None, None)
        except ProductPolicyMissingError:
            return _health_issue(
                issue_id="policy-uninitialized",
                severity="critical",
                category="policy",
                title="Product Policy Store is not initialized",
                detail="Daemon runtime will fail closed until global Product Policy is imported.",
                detected_at=detected_at,
                links=[{"type": "policy", "id": "global"}],
                recommended_actions=["import_config"],
            )
        except ProductPolicyInvalidError as exc:
            return _health_issue(
                issue_id="policy-invalid",
                severity="critical",
                category="policy",
                title="Product Policy Store is invalid",
                detail=str(exc),
                detected_at=detected_at,
                links=[{"type": "policy", "id": "global"}],
                recommended_actions=["inspect_policy", "import_config"],
            )
        return None

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
        if not self.store.path.exists():
            return {
                "status": "missing",
                "available": False,
                "schema_initialized": False,
            }
        try:
            conn = sqlite3.connect(_store_read_uri(self.store.path), uri=True)
        except sqlite3.OperationalError:
            return {
                "status": "unreadable",
                "available": False,
                "schema_initialized": False,
            }
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            schema_initialized = _has_core_schema(conn)
        except sqlite3.DatabaseError:
            conn.close()
            return {
                "status": "schema_incompatible",
                "available": False,
                "schema_initialized": False,
            }
        conn.close()
        if not schema_initialized:
            return {
                "status": "schema_uninitialized",
                "available": False,
                "schema_initialized": False,
            }
        return {
            "status": "available",
            "available": True,
            "schema_initialized": True,
        }


class _ReadOnlyProductPolicyRepository:
    def __init__(self, service: OperatorQueryService):
        self.service = service

    def get_product_policy(self) -> dict[str, Any] | None:
        return self.service._get_product_policy()

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        return self.service._get_chat_product_policy(chat_id)


def _policy_audit_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data["id"],
        "scope": data["scope"],
        "policy_key": data["policy_key"],
        "actor": data["actor"],
        "reason": data["reason"],
        "created_at": data["created_at"],
        "old_summary": _policy_summary(_loads_json_object(data["old_json"])),
        "new_summary": _policy_summary(_loads_json_object(data["new_json"])),
    }


def _policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    if not policy:
        return {}
    allowed = (
        "chat_id",
        "name",
        "auto_reply",
        "bot_joined",
        "reply_identity",
        "allow_user_fallback",
        "resource_download",
        "reply_policy",
        "default_chat_policy",
    )
    return {key: policy[key] for key in allowed if key in policy}


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


def _empty_effective_policy(
    policy_source: str, *, error: str | None = None
) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "policy_source": policy_source,
        "auto_reply": None,
        "bot_joined": None,
        "reply_identity": None,
        "allow_user_fallback": None,
        "resource_download": None,
    }
    if error is not None:
        dto["error"] = error
    return dto


def _recent_errors(
    failed_commands: list[dict[str, Any]],
    failed_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for command in failed_commands:
        summary = _approval_command_summary(command)
        errors.append(
            {
                "type": "approval_command",
                "status": summary["status"],
                "message": summary["label"],
                "updated_at": summary["updated_at"],
            }
        )
    for action in failed_actions:
        errors.append(
            {
                "type": "dispatch_action",
                "status": action["status"],
                "message": f"{action['kind']} action {action['action_id']}",
                "updated_at": action["updated_at"],
            }
        )
    return sorted(errors, key=lambda item: str(item["updated_at"]), reverse=True)


def _health_response(
    *,
    generated_at: str,
    runtime: dict[str, Any],
    issues: list[dict[str, Any]],
    open_issue_count: int | None = None,
    recent_failed_commands: list[dict[str, Any]],
    recent_failed_dispatch_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    highest = "info"
    if issues:
        highest = max(
            issues, key=lambda issue: _SEVERITY_ORDER.get(issue["severity"], 0)
        )["severity"]
    return {
        "generated_at": generated_at,
        "summary": {
            "highest_severity": highest,
            "open_issue_count": len(issues)
            if open_issue_count is None
            else open_issue_count,
        },
        "runtime": runtime,
        "issues": issues,
        "recent_failed_commands": recent_failed_commands,
        "recent_failed_dispatch_actions": recent_failed_dispatch_actions,
    }


def _health_issue(
    *,
    issue_id: str,
    severity: str,
    category: str,
    title: str,
    detail: str,
    detected_at: str | None,
    links: list[dict[str, str]] | None = None,
    recommended_actions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": _safe_detail(detail, fallback=title),
        "detected_at": detected_at,
        "links": links or [],
        "recommended_actions": recommended_actions or [],
    }


def _daemon_health_issue(
    liveness: dict[str, Any], *, detected_at: str
) -> dict[str, Any] | None:
    status = str(liveness.get("status") or "unknown")
    if status == "live":
        return None
    if status == "not_started":
        return _health_issue(
            issue_id="daemon-not-started",
            severity="warning",
            category="daemon",
            title="Daemon has not started",
            detail="No daemon tick has been recorded in the operator store.",
            detected_at=detected_at,
            recommended_actions=["run_doctor", "start_daemon"],
        )
    if status == "stale":
        return _health_issue(
            issue_id="daemon-stale",
            severity="error",
            category="daemon",
            title="Daemon heartbeat is stale",
            detail="The last running daemon heartbeat is older than the configured liveness threshold.",
            detected_at=detected_at,
            recommended_actions=["inspect_runtime", "restart_daemon"],
        )
    if status == "stopped":
        return _health_issue(
            issue_id="daemon-stopped",
            severity="warning",
            category="daemon",
            title="Daemon is not running",
            detail="The latest daemon run is not currently marked running.",
            detected_at=detected_at,
            recommended_actions=["inspect_runtime", "start_daemon"],
        )
    return _health_issue(
        issue_id="daemon-unknown",
        severity="warning",
        category="daemon",
        title="Daemon liveness is unknown",
        detail="The operator store does not contain enough daemon heartbeat data.",
        detected_at=detected_at,
        recommended_actions=["inspect_runtime"],
    )


def _health_check_issue(row: sqlite3.Row) -> dict[str, Any] | None:
    data = _row_dict(row)
    check_name = str(data.get("check_name") or "runtime")
    status = str(data.get("status") or "warning")
    severity = _health_check_severity(str(data.get("severity") or "warning"), status)
    detected_at = data.get("checked_at")
    return _health_issue(
        issue_id=f"health-{_slug(check_name)}-{_slug(str(detected_at or 'unknown'))}",
        severity=severity,
        category="runtime",
        title=f"{check_name} reported {status}",
        detail=str(
            data.get("message") or "Runtime health check reported a non-ok status."
        ),
        detected_at=detected_at,
        recommended_actions=["inspect_runtime"],
    )


def _health_warning_dto(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    data["message"] = _safe_detail(
        str(data.get("message") or ""),
        fallback="Runtime health check reported a non-ok status.",
    )
    return data


def _approval_command_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = _json_row_dict(row, "result_json")
    else:
        data = dict(row)
    if "label" in data and "result_summary" in data:
        return data
    verb, target_id = _parse_approval_command(str(data.get("command") or ""))
    result = (
        data.get("result_json") if isinstance(data.get("result_json"), dict) else {}
    )
    summary: dict[str, Any] = {
        "message_id": data.get("message_id"),
        "verb": verb,
        "target_id": target_id,
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "label": _approval_command_label(verb, target_id),
        "result_summary": _approval_command_result_summary(result),
    }
    summary["command"] = summary["label"]
    return summary


def _parse_approval_command(command_text: str) -> tuple[str | None, str | None]:
    parts = command_text.split(maxsplit=2)
    if not parts:
        return None, None
    verb = parts[0].lstrip("/") or None
    target_id = parts[1] if len(parts) >= 2 else None
    return verb, target_id


def _approval_command_label(verb: str | None, target_id: str | None) -> str:
    if verb and target_id:
        return f"/{verb} {target_id}"
    if verb:
        return f"/{verb}"
    return "approval command"


def _approval_command_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    error = result.get("error")
    if error is not None:
        summary["error"] = _safe_detail(str(error), fallback="Approval command failed.")
    for key in ("action_id", "notification_action_id"):
        if key in result:
            summary[key] = result[key]
    pending_ids = result.get("pending_approval_ids")
    if isinstance(pending_ids, list):
        summary["pending_approval_count"] = len(pending_ids)
    return summary


def _dispatch_action_issue(
    action: dict[str, Any], *, detected_at: str
) -> dict[str, Any]:
    status = str(action.get("status") or "")
    action_id = str(action.get("action_id"))
    is_needs_review = status == ActionStatus.FAILED_NEEDS_REVIEW.value
    title = (
        "Dispatch action needs review" if is_needs_review else "Dispatch action failed"
    )
    detail = (
        f"Action {action_id} failed after the actual-send boundary."
        if is_needs_review
        else f"Action {action_id} failed before it could be marked sent."
    )
    return _health_issue(
        issue_id=f"dispatch-action-{action_id}",
        severity="error",
        category="dispatch",
        title=title,
        detail=detail,
        detected_at=action.get("updated_at") or detected_at,
        links=[{"type": "dispatch_action", "id": action_id}],
        recommended_actions=_dispatch_action_names(status),
    )


def _stale_sending_issue(action: dict[str, Any], *, detected_at: str) -> dict[str, Any]:
    action_id = str(action.get("action_id"))
    return _health_issue(
        issue_id=f"dispatch-action-{action_id}-stale-sending",
        severity="warning",
        category="dispatch",
        title="Dispatch action is stale while sending",
        detail=f"Action {action_id} is still marked sending after the recovery threshold.",
        detected_at=action.get("updated_at") or detected_at,
        links=[{"type": "dispatch_action", "id": action_id}],
        recommended_actions=["inspect", "mark_sent", "cancel"],
    )


def _dispatch_action_names(status: str) -> list[str]:
    if status == ActionStatus.FAILED_NEEDS_REVIEW.value:
        return ["inspect", "mark_sent", "retry", "cancel"]
    if status == ActionStatus.FAILED.value:
        return ["inspect", "retry", "cancel"]
    return ["inspect"]


def _health_check_severity(severity: str, status: str) -> str:
    if severity == "critical":
        return "critical"
    if status == "failed":
        return "error"
    return "warning"


def _store_issue_title(status: str) -> str:
    return {
        "missing": "SQLite store is missing",
        "unreadable": "SQLite store is unreadable",
        "schema_uninitialized": "SQLite store schema is not initialized",
        "schema_incompatible": "SQLite store schema is incompatible",
    }.get(status, "SQLite store is unavailable")


def _store_issue_detail(status: str) -> str:
    return {
        "missing": "The operator store file does not exist yet, so console read models are unavailable.",
        "unreadable": "The operator store exists but cannot be opened by the local console.",
        "schema_uninitialized": "The operator store exists but required tables are missing.",
        "schema_incompatible": "The operator store could not be inspected with the expected schema contract.",
    }.get(status, "The operator store is unavailable.")


def _safe_detail(value: str, *, fallback: str) -> str:
    text = value.strip() or fallback
    return _ABSOLUTE_PATH_PATTERN.sub("[path]", text)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "unknown"


def _store_read_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _run_runtime_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "dry_run": bool(row["dry_run"]),
        "last_heartbeat_at": row["last_heartbeat_at"],
        "last_tick_started_at": row["last_tick_started_at"],
        "last_tick_finished_at": row["last_tick_finished_at"],
        "last_tick_status": row["last_tick_status"],
    }


def _daemon_liveness(
    last_run: dict[str, Any] | None,
    *,
    now: str,
    stale_after_seconds: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {"stale_after_seconds": stale_after_seconds}
    if last_run is None:
        return base | {"status": "not_started", "stale": False}
    run_status = last_run.get("status")
    heartbeat = last_run.get("last_heartbeat_at")
    base |= {
        "run_id": last_run.get("run_id"),
        "run_status": run_status,
        "last_heartbeat_at": heartbeat,
    }
    if run_status != "running":
        return base | {"status": "stopped", "stale": False}
    heartbeat_dt = _parse_datetime_or_none(heartbeat)
    now_dt = _parse_datetime_or_none(now)
    if heartbeat_dt is None or now_dt is None:
        return base | {
            "status": "stale",
            "stale": True,
            "reason": "missing_or_invalid_heartbeat",
        }
    age_seconds = max(0, int((now_dt - heartbeat_dt).total_seconds()))
    stale = age_seconds > stale_after_seconds
    return base | {
        "status": "stale" if stale else "live",
        "stale": stale,
        "heartbeat_age_seconds": age_seconds,
    }


def _global_policy_from_import_source(config: AppConfig) -> dict[str, Any]:
    default_chat_policy = ChatPolicyConfig().model_dump(mode="json")
    return {
        "reply_policy": config.reply_policy.model_dump(mode="json"),
        "default_chat_policy": {
            key: default_chat_policy[key]
            for key in (
                "bot_joined",
                "reply_identity",
                "allow_user_fallback",
                "resource_download",
            )
        },
    }


def _chat_policy_from_import_source(
    chat_id: str, config: ChatPolicyConfig
) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    return {"chat_id": chat_id, **data}


def _chat_policy_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "name": row["name"] or "",
        "auto_reply": bool(row["auto_reply"]),
        "bot_joined": bool(row["bot_joined"]),
        "reply_identity": row["reply_identity"],
        "allow_user_fallback": bool(row["allow_user_fallback"]),
        "resource_download": bool(row["resource_download"]),
    }


def _chat_policy_runtime_dto(row: sqlite3.Row) -> dict[str, Any]:
    return _chat_policy_from_row(row) | {"updated_at": row["updated_at"]}


def _settings_values(
    config: AppConfig,
    *,
    policy_status: dict[str, Any],
    global_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    values = {
        key: _config_path_value(config, path)
        for key, path in CONFIG_VALUE_PATHS.items()
    }
    values["policy.status.initialized"] = policy_status["initialized"]
    values["policy.status.import_diff"] = policy_status["policy_import_diff"]
    values["policy.audit.history"] = None
    values["policy.import_config"] = {"available": True}

    reply_policy = _nested_dict(global_policy, "reply_policy")
    default_chat_policy = _nested_dict(global_policy, "default_chat_policy")
    values["policy.global.p2p_auto_reply"] = reply_policy.get("p2p_auto_reply")
    values["policy.global.unknown_group_auto_reply"] = reply_policy.get(
        "unknown_group_auto_reply"
    )
    values["policy.global.default_bot_joined"] = default_chat_policy.get("bot_joined")
    values["policy.global.default_reply_identity"] = default_chat_policy.get(
        "reply_identity"
    )
    values["policy.global.default_allow_user_fallback"] = default_chat_policy.get(
        "allow_user_fallback"
    )
    values["policy.global.default_resource_download"] = default_chat_policy.get(
        "resource_download"
    )
    return values


def _config_path_value(config: AppConfig, path: str) -> Any:
    current: Any = config
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part)
    return current


def _nested_dict(value: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def _id_lookup(alias: str, value: int | str) -> tuple[str, list[Any]]:
    if isinstance(value, int):
        return f"{alias}.id = ?", [value]
    text = str(value)
    if text.isdigit():
        return f"{alias}.id = ?", [int(text)]
    return f"{alias}.short_id = ?", [text]


def _has_core_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ({})
        """.format(",".join("?" for _ in _CORE_TABLES)),
        tuple(sorted(_CORE_TABLES)),
    ).fetchall()
    return {row["name"] for row in rows} == _CORE_TABLES


def _minus_seconds(value: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(value)
    except ValueError:
        base = datetime.now().astimezone()
    return (
        (base - timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")
    )
