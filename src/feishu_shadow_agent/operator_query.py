from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from .config import AppConfig
from .membership import effective_membership_status
from .operator_queries.common import (
    OperatorQueryReadError,
    OperatorQueryUnavailable,
    ReadStoreUnavailable,
    action_dto,
    agent_audit_dto,
    approval_dto,
    attempt_dto,
    coerce_limit,
    coerce_offset,
    dispatch_recommended_actions,
    has_core_schema,
    message_dto,
    readback_summary,
    task_summary_dto,
)
from .operator_queries.feedback import FeedbackExecutionMode, FeedbackQuery
from .operator_queries.health import (
    RUN_RUNTIME_COLUMNS,
    HealthQuery,
    daemon_liveness,
    recent_errors,
    run_runtime_summary,
    store_read_uri,
)
from .operator_queries.message_detail import MessageDetailQuery
from .operator_queries.policy import PolicyQuery
from .store.sqlite_store import (
    RUN_HEARTBEAT_STALE_AFTER_SECONDS,
    SQLiteStore,
)
from .time_utils import parse_instant_or_none, shift_instant
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
        self._feedback_query = FeedbackQuery(connect=self._connect, now=self._now)
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
                    # This selects a module-level constant column list.
                    f"SELECT {RUN_RUNTIME_COLUMNS} FROM runs ORDER BY started_at DESC LIMIT 1"  # noqa: S608
                ).fetchone()
                daemon_run = conn.execute(
                    # This selects a module-level constant column list.
                    f"""
                    SELECT {RUN_RUNTIME_COLUMNS}
                    FROM runs
                    WHERE last_tick_started_at IS NOT NULL
                    ORDER BY last_tick_started_at DESC, started_at DESC
                    LIMIT 1
                    """,  # noqa: S608
                ).fetchone()
        except ReadStoreUnavailable:
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
        attention_summary, attention_tasks = self._attention_work(
            now=now,
            stale_after_seconds=stale_after_seconds,
            limit=limit,
        )
        return {
            "daemon_liveness": daemon_liveness(
                run_runtime_summary(daemon_run) if daemon_run else None,
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
            "attention_summary": attention_summary,
            "attention_tasks": attention_tasks,
            "ingestion_status": self.ingestion_status(now=now),
            "bot_membership_status": self.bot_membership_status(now=now),
            "recent_health_warnings": self._recent_health_warnings(limit=limit),
            "recent_errors": recent_errors(failed_commands, failed_or_needs_review),
            "last_run": run_runtime_summary(last_run) if last_run else None,
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

    def _attention_work(
        self, *, now: str, stale_after_seconds: int, limit: int
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        stale_cutoff = shift_instant(now, delta=timedelta(seconds=-stale_after_seconds))
        empty = {
            "pending_approval_count": 0,
            "overdue_approval_count": 0,
            "failed_action_count": 0,
            "uncertain_action_count": 0,
            "blocked_processing_count": 0,
            "failed_processing_count": 0,
            "affected_task_count": 0,
            "total_item_count": 0,
        }
        try:
            with self._connect() as conn:
                counts = conn.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM approvals WHERE status = 'pending') AS pending_approval_count,
                      (SELECT COUNT(*) FROM approvals
                       WHERE status = 'pending' AND expires_at IS NOT NULL
                         AND julianday(expires_at) < julianday(?)) AS overdue_approval_count,
                      (SELECT COUNT(*) FROM actions WHERE status = 'failed') AS failed_action_count,
                      (SELECT COUNT(*) FROM actions
                       WHERE status = 'failed_needs_review'
                          OR (status = 'sending' AND julianday(updated_at) <= julianday(?))) AS uncertain_action_count,
                      (SELECT COUNT(*) FROM message_processing
                       WHERE status = 'blocked_waiting_external') AS blocked_processing_count,
                      (SELECT COUNT(*) FROM message_processing
                       WHERE status = 'processing_failed_terminal') AS failed_processing_count,
                      (SELECT COUNT(DISTINCT task_id) FROM (
                         SELECT task_id FROM approvals WHERE status = 'pending'
                         UNION ALL
                         SELECT task_id FROM actions
                         WHERE status IN ('failed', 'failed_needs_review')
                            OR (status = 'sending' AND julianday(updated_at) <= julianday(?))
                         UNION ALL
                         SELECT task_id FROM message_processing
                         WHERE status IN ('blocked_waiting_external', 'processing_failed_terminal')
                       ) WHERE task_id IS NOT NULL) AS affected_task_count
                    """,
                    (now, stale_cutoff, stale_cutoff),
                ).fetchone()
                tasks = conn.execute(
                    """
                    WITH approval_counts AS (
                      SELECT task_id,
                             COUNT(*) AS pending_approval_count,
                             SUM(CASE WHEN expires_at IS NOT NULL
                                          AND julianday(expires_at) < julianday(?)
                                      THEN 1 ELSE 0 END) AS overdue_approval_count,
                             MAX(created_at) AS latest_at
                      FROM approvals
                      WHERE status = 'pending' AND task_id IS NOT NULL
                      GROUP BY task_id
                    ), action_counts AS (
                      SELECT task_id,
                             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_action_count,
                             SUM(CASE WHEN status = 'failed_needs_review'
                                           OR (status = 'sending' AND julianday(updated_at) <= julianday(?))
                                      THEN 1 ELSE 0 END) AS uncertain_action_count,
                             MAX(updated_at) AS latest_at
                      FROM actions
                      WHERE task_id IS NOT NULL
                        AND (status IN ('failed', 'failed_needs_review')
                             OR (status = 'sending' AND julianday(updated_at) <= julianday(?)))
                      GROUP BY task_id
                    ), processing_counts AS (
                      SELECT task_id,
                             SUM(CASE WHEN status = 'blocked_waiting_external' THEN 1 ELSE 0 END) AS blocked_processing_count,
                             SUM(CASE WHEN status = 'processing_failed_terminal' THEN 1 ELSE 0 END) AS failed_processing_count,
                             MAX(updated_at) AS latest_at
                      FROM message_processing
                      WHERE task_id IS NOT NULL
                        AND status IN ('blocked_waiting_external', 'processing_failed_terminal')
                      GROUP BY task_id
                    )
                    SELECT t.id AS task_id, t.short_id AS task_short_id, t.task_label,
                           t.chat_id, t.status,
                           COALESCE(ap.pending_approval_count, 0) AS pending_approval_count,
                           COALESCE(ap.overdue_approval_count, 0) AS overdue_approval_count,
                           COALESCE(ac.failed_action_count, 0) AS failed_action_count,
                           COALESCE(ac.uncertain_action_count, 0) AS uncertain_action_count,
                           COALESCE(pc.blocked_processing_count, 0) AS blocked_processing_count,
                           COALESCE(pc.failed_processing_count, 0) AS failed_processing_count,
                           MAX(COALESCE(ap.latest_at, ''), COALESCE(ac.latest_at, ''),
                               COALESCE(pc.latest_at, ''), t.updated_at) AS latest_at
                    FROM tasks t
                    LEFT JOIN approval_counts ap ON ap.task_id = t.id
                    LEFT JOIN action_counts ac ON ac.task_id = t.id
                    LEFT JOIN processing_counts pc ON pc.task_id = t.id
                    WHERE COALESCE(ap.pending_approval_count, 0)
                        + COALESCE(ac.failed_action_count, 0)
                        + COALESCE(ac.uncertain_action_count, 0)
                        + COALESCE(pc.blocked_processing_count, 0)
                        + COALESCE(pc.failed_processing_count, 0) > 0
                    ORDER BY latest_at DESC, t.id DESC
                    LIMIT ?
                    """,
                    (now, stale_cutoff, stale_cutoff, coerce_limit(limit)),
                ).fetchall()
        except ReadStoreUnavailable:
            return empty, []
        summary = {
            key: int(counts[key] or 0) for key in empty if key != "total_item_count"
        }
        summary["total_item_count"] = sum(
            summary[key]
            for key in (
                "pending_approval_count",
                "failed_action_count",
                "uncertain_action_count",
                "blocked_processing_count",
                "failed_processing_count",
            )
        )
        return summary, [_row_attention_task(row) for row in tasks]

    def ingestion_status(self, *, now: str | None = None) -> dict[str, Any]:
        observed_at = now or self._now()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT key, value_json, updated_at
                    FROM checkpoints
                    WHERE (key = 'approval_inbox'
                           OR key IN ('ingest.group_at_me', 'ingest.p2p')
                           OR key LIKE 'active_watch.%')
                      AND key NOT LIKE 'ingest.scheduler.%'
                    ORDER BY key
                    """
                ).fetchall()
        except ReadStoreUnavailable:
            rows = []
        sources: list[dict[str, Any]] = []
        checkpoint_ages: list[int] = []
        backlog_count = 0
        budget_exhausted_count = 0
        for row in rows:
            try:
                decoded = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            value = cast(dict[str, Any], decoded) if isinstance(decoded, dict) else {}
            backlog_value = value.get("backlog")
            backlog = (
                cast(dict[str, Any], backlog_value)
                if isinstance(backlog_value, dict)
                else None
            )
            last_drain_value = value.get("last_drain")
            last_drain = (
                cast(dict[str, Any], last_drain_value)
                if isinstance(last_drain_value, dict)
                else None
            )
            last_success_at = value.get("last_success_at")
            checkpoint_age_seconds: int | None = None
            last_success = parse_instant_or_none(last_success_at)
            observed = parse_instant_or_none(observed_at)
            if last_success is not None and observed is not None:
                checkpoint_age_seconds = max(
                    0, int((observed - last_success).total_seconds())
                )
                checkpoint_ages.append(checkpoint_age_seconds)
            if backlog is not None:
                backlog_count += 1
                if backlog.get("reason") == "tick_budget_exhausted":
                    budget_exhausted_count += 1
            sources.append(
                {
                    "checkpoint_key": row["key"],
                    "updated_at": row["updated_at"],
                    "last_success_at": last_success_at,
                    "checkpoint_age_seconds": checkpoint_age_seconds,
                    "drain_complete": backlog is None,
                    "backlog": backlog,
                    "last_drain": last_drain,
                }
            )
        return {
            "summary": {
                "source_count": len(sources),
                "backlog_count": backlog_count,
                "budget_exhausted_count": budget_exhausted_count,
                "oldest_checkpoint_age_seconds": (
                    max(checkpoint_ages) if checkpoint_ages else None
                ),
            },
            "sources": sources,
        }

    def bot_membership_status(self, *, now: str | None = None) -> dict[str, Any]:
        observed_at = now or self._now()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT key, value_json, updated_at
                    FROM checkpoints
                    WHERE key LIKE 'runtime.bot_membership.%'
                    ORDER BY key
                    """
                ).fetchall()
                candidate_rows = conn.execute(
                    """
                    SELECT chat_id FROM chat_policies
                    UNION
                    SELECT chat_id FROM tasks
                    WHERE chat_type = 'group' AND chat_id IS NOT NULL
                    ORDER BY chat_id
                    """
                ).fetchall()
        except ReadStoreUnavailable:
            rows = []
            candidate_rows = []
        facts: list[dict[str, Any]] = []
        counts = {"present": 0, "absent": 0, "unknown": 0, "unobserved": 0}
        prefix = "runtime.bot_membership."
        observed_chats: set[str] = set()
        for row in rows:
            try:
                decoded = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            fact = cast(dict[str, Any], decoded) if isinstance(decoded, dict) else {}
            status = effective_membership_status(fact, now=observed_at)
            chat_id = str(row["key"])[len(prefix) :]
            observed_chats.add(chat_id)
            counts[status] += 1
            facts.append(
                {
                    "chat_id": chat_id,
                    "status": status,
                    "observed_status": fact.get("status"),
                    "checked_at": fact.get("checked_at"),
                    "next_probe_at": fact.get("next_probe_at"),
                    "source": fact.get("source"),
                    "error": fact.get("error"),
                    "updated_at": row["updated_at"],
                }
            )
        for row in candidate_rows:
            chat_id = str(row["chat_id"])
            if chat_id in observed_chats:
                continue
            counts["unobserved"] += 1
            facts.append(
                {
                    "chat_id": chat_id,
                    "status": "unobserved",
                    "observed_status": None,
                    "checked_at": None,
                    "next_probe_at": None,
                    "source": None,
                    "error": None,
                    "updated_at": None,
                }
            )
        facts.sort(key=lambda fact: str(fact["chat_id"]))
        return {"summary": counts, "facts": facts}

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

    def feedback_overview(
        self,
        *,
        execution_mode: FeedbackExecutionMode = "production",
        recent_limit: int = 50,
    ) -> dict[str, Any]:
        return self._feedback_query.overview(
            windows=(7, 30),
            recent_days=30,
            recent_limit=recent_limit,
            execution_mode=execution_mode,
        )

    def list_feedback(
        self,
        *,
        days: int = 30,
        limit: int = 50,
        offset: int = 0,
        execution_mode: FeedbackExecutionMode = "production",
    ) -> list[dict[str, Any]]:
        return self._feedback_query.list_feedback(
            days=days,
            limit=limit,
            offset=offset,
            execution_mode=execution_mode,
        )

    def list_approvals(
        self,
        *,
        status: str | None = None,
        statuses: Iterable[str] | None = None,
        task_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        status_values = tuple(statuses) if statuses is not None else ()
        if status_values:
            where.append(f"a.status IN ({','.join('?' for _ in status_values)})")
            params.extend(status_values)
        elif status is not None:
            where.append("a.status = ?")
            params.append(status)
        if task_id is not None:
            where.append("a.task_id = ?")
            params.append(task_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([coerce_limit(limit), coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    # `where_sql` is assembled from fixed query fragments; all
                    # caller values remain bound parameters.
                    f"""
                    SELECT a.id, a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.status,
                           a.payload_json, a.preview, a.source_message_id, a.source_revision,
                           a.created_at, a.expires_at, a.resolved_at
                    FROM approvals a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    {where_sql}
                    ORDER BY a.created_at DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,  # noqa: S608
                    params,
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        now = self._now()
        return [approval_dto(row, now=now) for row in rows]

    def approval_detail(self, approval_id: int | str) -> dict[str, Any] | None:
        where_sql, params = _id_lookup("a", approval_id)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    # `_id_lookup` returns fixed SQL fragments and bound values.
                    f"""
                    SELECT a.id, a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.status,
                           a.payload_json, a.preview, a.source_message_id, a.source_revision,
                           a.created_at, a.expires_at, a.resolved_at
                    FROM approvals a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE {where_sql}
                    """,  # noqa: S608
                    params,
                ).fetchone()
        except ReadStoreUnavailable:
            return None
        if row is None:
            return None
        return approval_dto(row, now=self._now(), include_payload=True)

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
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("t.status = ?")
            params.append(status)
        if chat_id is not None:
            where.append("t.chat_id = ?")
            params.append(chat_id)
        if active_only:
            where.append(
                "(t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))"
            )
            params.append(now)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([coerce_limit(limit), coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    # `where_sql` is assembled from fixed query fragments; all
                    # caller values remain bound parameters.
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
                               AND julianday(ap.expires_at) < julianday(?)
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
                    """,  # noqa: S608
                    [now, *params],
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        return [task_summary_dto(row) for row in rows]

    def task_detail(
        self, task_id: int | str, *, limit: int = 20
    ) -> dict[str, Any] | None:
        where_sql, params = _id_lookup("t", task_id)
        try:
            with self._connect() as conn:
                task = conn.execute(
                    # `_id_lookup` returns fixed SQL fragments and bound values.
                    f"""
                    SELECT t.*, COUNT(tm.message_id) AS message_count
                    FROM tasks t
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    WHERE {where_sql}
                    GROUP BY t.id
                    """,  # noqa: S608
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
                    (int(task["id"]), coerce_limit(limit)),
                ).fetchall()
                agent_audit_rows = conn.execute(
                    """
                    SELECT id, backend_provider, request_type, prompt_version, prompt_hash,
                           task_id, agent_session_id,
                           input_message_ids_json, input_message_revisions_json,
                           input_resource_ids_json, response_json,
                           error, latency_ms, prompt_json, tool_permissions_profile, created_at
                    FROM agent_audits
                    WHERE task_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (int(task["id"]), coerce_limit(limit)),
                ).fetchall()
                processing_rows = conn.execute(
                    """
                    SELECT mp.*,
                           pra.id AS retry_id,
                           pra.status AS retry_status,
                           pra.actor AS retry_actor,
                           pra.reason AS retry_reason,
                           pra.error AS retry_error,
                           pra.created_at AS retry_created_at,
                           pra.finished_at AS retry_finished_at
                    FROM message_processing mp
                    LEFT JOIN processing_retry_attempts pra ON pra.id = (
                      SELECT latest.id FROM processing_retry_attempts latest
                      WHERE latest.message_id = mp.message_id
                        AND latest.revision = mp.revision
                        AND latest.stage = mp.stage
                      ORDER BY latest.id DESC LIMIT 1
                    )
                    WHERE mp.task_id = ?
                    ORDER BY mp.updated_at DESC, mp.id DESC
                    LIMIT ?
                    """,
                    (int(task["id"]), coerce_limit(limit)),
                ).fetchall()
        except ReadStoreUnavailable:
            return None
        task_summary = task_summary_dto(task)
        pending_approvals = self.list_approvals(
            status=ApprovalStatus.PENDING.value,
            task_id=int(task["id"]),
            limit=limit,
        )
        actions = self.list_dispatch_actions(task_id=int(task["id"]), limit=limit)
        return {
            **task_summary,
            "recent_messages": [message_dto(row) for row in reversed(messages)],
            "pending_approvals": pending_approvals,
            "actions": actions,
            "agent_audits": [agent_audit_dto(row) for row in agent_audit_rows],
            "processing": [_task_processing_dto(row) for row in processing_rows],
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
        where: list[str] = []
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
        params.extend([coerce_limit(limit), coerce_offset(offset)])
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    # `where_sql` is assembled from fixed query fragments; all
                    # caller values remain bound parameters.
                    f"""
                    SELECT a.*, t.short_id AS task_short_id
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    {where_sql}
                    ORDER BY a.updated_at DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,  # noqa: S608
                    params,
                ).fetchall()
        except ReadStoreUnavailable:
            return []
        return [action_dto(row, include_payload=False) for row in rows]

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
        except ReadStoreUnavailable:
            return None
        attempt_dtos = [attempt_dto(row) for row in attempts]
        action_data = action_dto(action, include_payload=True)
        return {
            "action": action_data,
            "attempts": attempt_dtos,
            "readback_summary": readback_summary(attempt_dtos),
            "recommended_actions": dispatch_recommended_actions(action_data),
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
        return self._health_query.failed_approval_commands(limit=limit)

    def _recent_health_warnings(self, *, limit: int) -> list[dict[str, Any]]:
        return self._health_query.recent_health_warnings(limit=limit)

    def _stale_sending_actions(
        self, *, stale_after_seconds: int, limit: int
    ) -> list[dict[str, Any]]:
        return self._health_query.stale_sending_actions(
            stale_after_seconds=stale_after_seconds,
            limit=limit,
        )

    def _get_product_policy(self) -> dict[str, Any] | None:
        return self._policy_query.get_product_policy()

    def _get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        return self._policy_query.get_chat_product_policy(chat_id)

    def _list_chat_product_policies(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._policy_query.list_chat_product_policies(limit=limit)

    def _policy_import_diff(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return self._policy_query.policy_import_diff(conn)

    def _missing_store_policy_import_diff(self) -> dict[str, Any]:
        return self._policy_query.missing_store_policy_import_diff()

    def _policy_health_issue(
        self, policy_status: dict[str, Any], *, detected_at: str
    ) -> dict[str, Any] | None:
        return self._health_query.policy_health_issue(
            policy_status,
            detected_at=detected_at,
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.store.path.exists():
            raise ReadStoreUnavailable("SQLite store does not exist.")
        uri = store_read_uri(self.store.path)
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as exc:
            raise ReadStoreUnavailable(str(exc)) from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        if not has_core_schema(conn):
            conn.close()
            raise ReadStoreUnavailable("SQLite store schema is not initialized.")
        return conn

    def _store_status(self) -> dict[str, Any]:
        return self._health_query.store_status()


def _task_recommended_actions(
    pending_approvals: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    status: str | None = None,
    task_id: str | None = None,
) -> list[str]:
    recommendations: list[str] = []
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


def _task_processing_dto(row: sqlite3.Row) -> dict[str, Any]:
    retry = None
    if row["retry_id"] is not None:
        retry = {
            "id": int(row["retry_id"]),
            "status": row["retry_status"],
            "actor": row["retry_actor"],
            "reason": row["retry_reason"],
            "error": row["retry_error"],
            "created_at": row["retry_created_at"],
            "finished_at": row["retry_finished_at"],
        }
    return {
        "id": int(row["id"]),
        "message_id": row["message_id"],
        "revision": int(row["revision"]),
        "task_id": None if row["task_id"] is None else int(row["task_id"]),
        "stage": row["stage"],
        "status": row["status"],
        "attempt_count": int(row["attempt_count"] or 0),
        "last_error": row["last_error"],
        "terminal_reason": row["terminal_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest_retry": retry,
    }


def _row_attention_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": int(row["task_id"]),
        "task_short_id": row["task_short_id"],
        "task_label": row["task_label"],
        "chat_id": row["chat_id"],
        "status": row["status"],
        "pending_approval_count": int(row["pending_approval_count"] or 0),
        "overdue_approval_count": int(row["overdue_approval_count"] or 0),
        "failed_action_count": int(row["failed_action_count"] or 0),
        "uncertain_action_count": int(row["uncertain_action_count"] or 0),
        "blocked_processing_count": int(row["blocked_processing_count"] or 0),
        "failed_processing_count": int(row["failed_processing_count"] or 0),
        "latest_at": row["latest_at"],
    }


def _id_lookup(alias: str, value: int | str) -> tuple[str, list[Any]]:
    if isinstance(value, int):
        return f"{alias}.id = ?", [value]
    text = str(value)
    if text.isdigit():
        return f"{alias}.id = ?", [int(text)]
    return f"{alias}.short_id = ?", [text]
