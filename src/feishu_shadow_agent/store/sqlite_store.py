from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..config import AppConfig, ChatPolicyConfig, ReplyPolicyConfig
from ..time_utils import normalize_instant, parse_instant_or_none, shift_instant
from ..types import (
    ActionKind,
    ActionRecord,
    ActionStatus,
    ApprovalKind,
    ApprovalOutcome,
    ApprovalStatus,
    DispatchAttemptRecord,
    DispatchAttemptStatus,
    DispatchClaim,
    DispatchErrorStage,
    ExecutionMode,
    FeedbackReason,
    HealthCheckResult,
    LifecycleStatePolicy,
    NormalizedMessage,
    ResourceRef,
    RouteDecision,
    RunTickStatus,
    TaskRecord,
    TaskStatus,
    new_run_id,
    utc_now_iso,
)

SQLITE_BUSY_TIMEOUT_MS = 5000
RUN_HEARTBEAT_STALE_AFTER_SECONDS = 300
PRODUCT_POLICY_KEY = "reply_policy"
LATEST_NON_OK_HEALTH_CHECKS_SQL = """
SELECT hc.check_name, hc.severity, hc.status, hc.message, hc.checked_at
FROM health_checks hc
WHERE hc.status != 'ok'
  AND NOT EXISTS (
      SELECT 1
      FROM health_checks newer
      WHERE newer.check_name = hc.check_name
        AND (
            julianday(newer.checked_at) > julianday(hc.checked_at)
            OR (
                julianday(newer.checked_at) = julianday(hc.checked_at)
                AND newer.id > hc.id
            )
        )
  )
ORDER BY julianday(hc.checked_at) DESC, hc.id DESC
LIMIT ?
"""


class SQLiteStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], str] = utc_now_iso):
        self.path = Path(path)
        self.clock = lambda: normalize_instant(clock())

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            schema = Path(__file__).with_name("schema.sql")
            conn.executescript(schema.read_text(encoding="utf-8"))

    def health_probe(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def get_product_policy(
        self, key: str = PRODUCT_POLICY_KEY
    ) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else json.loads(row["policy_json"])

    def get_chat_product_policy(self, chat_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return None if row is None else _chat_policy_from_row(row)

    def product_policy_initialization_probe(self) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
        missing = [] if row is not None else [f"global:{PRODUCT_POLICY_KEY}"]
        return {
            "initialized": not missing,
            "missing": missing,
        }

    def list_policy_audits(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, scope, policy_key, actor, old_json, new_json, reason, created_at
                FROM policy_audits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_json_row_dict(row, "old_json", "new_json") for row in rows]

    def import_product_policy_from_config(
        self,
        config: AppConfig,
        *,
        replace: bool = False,
        used_defaults: bool = False,
        actor: str = "import_config",
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        audit_reason = reason or (
            "policy import-config --replace" if replace else "policy import-config"
        )
        result: dict[str, Any] = {
            "status": "imported",
            "mode": "replace" if replace else "fill_missing",
            "used_defaults": used_defaults,
            "inserted": {"global": [], "chats": []},
            "skipped": {"global": [], "chats": []},
            "replaced": {"global": [], "chats": []},
            "audit_count": 0,
        }
        global_policy = _global_product_policy_from_config(config)
        with self.connect() as conn:
            existing_global = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            if existing_global is None:
                conn.execute(
                    """
                    INSERT INTO product_policies(key, policy_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (PRODUCT_POLICY_KEY, _policy_json(global_policy), now),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=PRODUCT_POLICY_KEY,
                    old_policy=None,
                    new_policy=global_policy,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["inserted"]["global"].append(PRODUCT_POLICY_KEY)
                result["audit_count"] += 1
            elif replace:
                old_policy = json.loads(existing_global["policy_json"])
                conn.execute(
                    """
                    UPDATE product_policies
                    SET policy_json = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (_policy_json(global_policy), now, PRODUCT_POLICY_KEY),
                )
                self._record_policy_audit_locked(
                    conn,
                    scope="global",
                    policy_key=PRODUCT_POLICY_KEY,
                    old_policy=old_policy,
                    new_policy=global_policy,
                    actor=actor,
                    reason=audit_reason,
                    now=now,
                )
                result["replaced"]["global"].append(PRODUCT_POLICY_KEY)
                result["audit_count"] += 1
            else:
                result["skipped"]["global"].append(PRODUCT_POLICY_KEY)

            for chat_id, chat_config in sorted(config.chats.items()):
                chat_policy = _chat_policy_from_config(chat_id, chat_config)
                existing_chat = conn.execute(
                    """
                    SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                           allow_user_fallback, resource_download
                    FROM chat_policies
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()
                if existing_chat is None:
                    self._insert_chat_policy_locked(conn, chat_policy, now=now)
                    self._record_policy_audit_locked(
                        conn,
                        scope="chat",
                        policy_key=f"chat:{chat_id}",
                        old_policy=None,
                        new_policy=chat_policy,
                        actor=actor,
                        reason=audit_reason,
                        now=now,
                    )
                    result["inserted"]["chats"].append(chat_id)
                    result["audit_count"] += 1
                elif replace:
                    old_policy = _chat_policy_from_row(existing_chat)
                    self._update_chat_policy_locked(conn, chat_policy, now=now)
                    self._record_policy_audit_locked(
                        conn,
                        scope="chat",
                        policy_key=f"chat:{chat_id}",
                        old_policy=old_policy,
                        new_policy=chat_policy,
                        actor=actor,
                        reason=audit_reason,
                        now=now,
                    )
                    result["replaced"]["chats"].append(chat_id)
                    result["audit_count"] += 1
                else:
                    result["skipped"]["chats"].append(chat_id)
        result["initialization"] = self.product_policy_initialization_probe()
        return result

    def update_product_policy(
        self,
        policy: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        new_policy = _normalize_global_product_policy(policy)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT policy_json FROM product_policies WHERE key = ?",
                (PRODUCT_POLICY_KEY,),
            ).fetchone()
            old_policy = (
                None if existing is None else json.loads(existing["policy_json"])
            )
            if old_policy == new_policy:
                return {
                    "scope": "global",
                    "policy_key": PRODUCT_POLICY_KEY,
                    "changed": False,
                    "old_policy": old_policy,
                    "new_policy": new_policy,
                    "audit_id": None,
                }
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO product_policies(key, policy_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (PRODUCT_POLICY_KEY, _policy_json(new_policy), now),
                )
            else:
                conn.execute(
                    """
                    UPDATE product_policies
                    SET policy_json = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (_policy_json(new_policy), now, PRODUCT_POLICY_KEY),
                )
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="global",
                policy_key=PRODUCT_POLICY_KEY,
                old_policy=old_policy,
                new_policy=new_policy,
                actor=actor,
                reason=reason or "policy update-global",
                now=now,
            )
        return {
            "scope": "global",
            "policy_key": PRODUCT_POLICY_KEY,
            "changed": True,
            "old_policy": old_policy,
            "new_policy": new_policy,
            "audit_id": audit_id,
        }

    def upsert_chat_product_policy(
        self,
        policy: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        new_policy = _normalize_chat_product_policy(policy)
        chat_id = str(new_policy["chat_id"])
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            old_policy = None if existing is None else _chat_policy_from_row(existing)
            if old_policy == new_policy:
                return {
                    "scope": "chat",
                    "policy_key": f"chat:{chat_id}",
                    "changed": False,
                    "old_policy": old_policy,
                    "new_policy": new_policy,
                    "audit_id": None,
                }
            if existing is None:
                self._insert_chat_policy_locked(conn, new_policy, now=now)
            else:
                self._update_chat_policy_locked(conn, new_policy, now=now)
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="chat",
                policy_key=f"chat:{chat_id}",
                old_policy=old_policy,
                new_policy=new_policy,
                actor=actor,
                reason=reason or "policy update-chat",
                now=now,
            )
        return {
            "scope": "chat",
            "policy_key": f"chat:{chat_id}",
            "changed": True,
            "old_policy": old_policy,
            "new_policy": new_policy,
            "audit_id": audit_id,
        }

    def delete_chat_product_policy(
        self,
        chat_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        normalized_chat_id = chat_id.strip()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT chat_id, name, auto_reply, bot_joined, reply_identity,
                       allow_user_fallback, resource_download
                FROM chat_policies
                WHERE chat_id = ?
                """,
                (normalized_chat_id,),
            ).fetchone()
            old_policy = None if existing is None else _chat_policy_from_row(existing)
            if old_policy is None:
                return {
                    "scope": "chat",
                    "policy_key": f"chat:{normalized_chat_id}",
                    "changed": False,
                    "old_policy": None,
                    "new_policy": None,
                    "audit_id": None,
                }
            conn.execute(
                "DELETE FROM chat_policies WHERE chat_id = ?",
                (normalized_chat_id,),
            )
            audit_id = self._record_policy_audit_locked(
                conn,
                scope="chat",
                policy_key=f"chat:{normalized_chat_id}",
                old_policy=old_policy,
                new_policy=None,
                actor=actor,
                reason=reason or "policy delete-chat",
                now=now,
            )
        return {
            "scope": "chat",
            "policy_key": f"chat:{normalized_chat_id}",
            "changed": True,
            "old_policy": old_policy,
            "new_policy": None,
            "audit_id": audit_id,
        }

    def record_run_start(
        self,
        *,
        run_id: str,
        dry_run: bool,
        git_commit: str | None = None,
        git_dirty: bool | None = None,
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs(
                  run_id, started_at, finished_at, status, dry_run, git_commit, git_dirty,
                  last_heartbeat_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    "running",
                    int(dry_run),
                    git_commit,
                    None if git_dirty is None else int(git_dirty),
                    now,
                ),
            )

    def record_run_finish(
        self,
        *,
        run_id: str,
        status: str,
        health_summary: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, health_summary_json = ?
                WHERE run_id = ?
                """,
                (
                    self.clock(),
                    status,
                    json.dumps(health_summary or {}, ensure_ascii=False, default=str),
                    run_id,
                ),
            )

    def record_run_tick_started(self, *, run_id: str, dry_run: bool) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(
                  run_id, started_at, finished_at, status, dry_run, last_heartbeat_at,
                  last_tick_started_at, last_tick_finished_at, last_tick_status,
                  last_tick_summary_json
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = 'running',
                  dry_run = excluded.dry_run,
                  last_heartbeat_at = excluded.last_heartbeat_at,
                  last_tick_started_at = excluded.last_tick_started_at,
                  last_tick_finished_at = NULL,
                  last_tick_status = excluded.last_tick_status,
                  last_tick_summary_json = excluded.last_tick_summary_json
                """,
                (
                    run_id,
                    now,
                    "running",
                    int(dry_run),
                    now,
                    now,
                    RunTickStatus.RUNNING.value,
                    "{}",
                ),
            )

    def record_run_tick_progress(
        self,
        *,
        run_id: str,
        summary: dict[str, Any],
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET last_heartbeat_at = ?,
                    last_tick_summary_json = ?
                WHERE run_id = ?
                """,
                (
                    self.clock(),
                    json.dumps(summary, ensure_ascii=False, default=str),
                    run_id,
                ),
            )

    def record_run_tick_finished(
        self,
        *,
        run_id: str,
        status: str,
        summary: dict[str, Any],
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET last_heartbeat_at = ?,
                    last_tick_finished_at = ?,
                    last_tick_status = ?,
                    last_tick_summary_json = ?
                WHERE run_id = ?
                """,
                (
                    now,
                    now,
                    status,
                    json.dumps(summary, ensure_ascii=False, default=str),
                    run_id,
                ),
            )

    def record_health_results(
        self,
        *,
        run_id: str | None,
        results: Iterable[HealthCheckResult],
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            if run_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs(run_id, started_at, status, dry_run)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, self.clock(), "running", 1),
                )
            conn.executemany(
                """
                INSERT INTO health_checks(
                  run_id, check_name, severity, status, message, details_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        result.name,
                        result.severity,
                        result.status,
                        result.message,
                        json.dumps(result.details, ensure_ascii=False, default=str),
                        self.clock(),
                    )
                    for result in results
                ],
            )

    def latest_health_check_status(self, check_name: str) -> str | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT status
                FROM health_checks
                WHERE check_name = ?
                ORDER BY julianday(checked_at) DESC, id DESC
                LIMIT 1
                """,
                (check_name,),
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def set_checkpoint(self, key: str, value: dict[str, Any]) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (
                    key,
                    json.dumps(value, ensure_ascii=False, default=str),
                    self.clock(),
                ),
            )

    def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM checkpoints WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def upsert_message(self, message: NormalizedMessage) -> bool:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            return self._upsert_message_locked(conn, message, now=now)

    def get_message(self, message_id: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()

    def get_messages_by_ids(self, message_ids: Iterable[str]) -> list[sqlite3.Row]:
        self.initialize()
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE message_id IN ({placeholders}) ORDER BY julianday(sent_at), message_id",
                ids,
            ).fetchall()
        by_id = {row["message_id"]: row for row in rows}
        return [by_id[message_id] for message_id in ids if message_id in by_id]

    def message_has_routing_audit(self, message_id: str) -> bool:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM routing_audits WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
        return row is not None

    def get_latest_non_duplicate_routing_decision(
        self,
        message_id: str,
    ) -> tuple[RouteDecision, TaskRecord | None] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM routing_audits
                WHERE message_id = ?
                  AND NOT (route = 'ignore' AND route_reason = 'duplicate_message')
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            target_task_id = row["target_task_id"] or row["task_id"]
            task = None
            if target_task_id is not None:
                task_row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (target_task_id,)
                ).fetchone()
                if task_row is not None:
                    task = _task_from_row(task_row)
            decision = RouteDecision(
                row["route"],
                target_task_id=None if target_task_id is None else int(target_task_id),
                target_task_short_id=None if task is None else task.short_id,
                reason=row["route_reason"] or "",
                candidates_count=int(row["candidates_count"] or 0),
                shortcut_hit=bool(row["shortcut_hit"]),
                router_called=bool(row["router_called"]),
                matched_by=row["matched_by"],
            )
        return decision, task

    def message_processing_is_final(self, message_id: str, *, stage: str) -> bool:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM message_processing
                WHERE message_id = ?
                  AND stage = ?
                  AND status IN ('processed', 'processing_failed_terminal', 'blocked_waiting_external')
                LIMIT 1
                """,
                (message_id, stage),
            ).fetchone()
        return row is not None

    def record_message_processing(
        self,
        *,
        message_id: str,
        stage: str,
        status: str,
        task_id: int | None = None,
        attempt_count: int = 0,
        last_error: str | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO message_processing(
                  message_id, task_id, stage, status, attempt_count, last_error,
                  terminal_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, stage) DO UPDATE SET
                  task_id = COALESCE(excluded.task_id, message_processing.task_id),
                  status = excluded.status,
                  attempt_count = excluded.attempt_count,
                  last_error = excluded.last_error,
                  terminal_reason = excluded.terminal_reason,
                  updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    task_id,
                    stage,
                    status,
                    attempt_count,
                    last_error,
                    terminal_reason,
                    now,
                    now,
                ),
            )

    def upsert_resource(
        self,
        resource: ResourceRef,
        *,
        download_status: str,
        path: str | None = None,
        sha256_hex: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        now = self.clock()
        payload = resource.raw | (raw or {})
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO resources(
                  message_id, file_key, resource_type, download_status, path, sha256, raw_json,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, file_key, resource_type) DO UPDATE SET
                  download_status = excluded.download_status,
                  path = excluded.path,
                  sha256 = excluded.sha256,
                  raw_json = excluded.raw_json,
                  updated_at = excluded.updated_at
                """,
                (
                    resource.message_id,
                    resource.file_key,
                    resource.resource_type,
                    download_status,
                    path,
                    sha256_hex,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )

    def count_prunable_message_raw_json(
        self, *, cutoff: str, replacement_json: str
    ) -> int:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM messages
                WHERE julianday(inserted_at) <= julianday(?)
                  AND raw_json != ?
                """,
                (cutoff, replacement_json),
            ).fetchone()
        return int(row["count"])

    def prune_message_raw_json(self, *, cutoff: str, replacement_json: str) -> int:
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages
                SET raw_json = ?
                WHERE julianday(inserted_at) <= julianday(?)
                  AND raw_json != ?
                """,
                (replacement_json, cutoff, replacement_json),
            )
        return int(cursor.rowcount)

    def list_prunable_resources(self, *, cutoff: str) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT r.*
                FROM resources r
                WHERE r.download_status = 'downloaded'
                  AND r.path IS NOT NULL
                  AND julianday(r.updated_at) <= julianday(?)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tasks t
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    WHERE (t.root_message_id = r.message_id OR tm.message_id = r.message_id)
                      AND t.status = 'watching'
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM approvals a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    WHERE a.status = 'pending'
                      AND (t.root_message_id = r.message_id OR tm.message_id = r.message_id)
                  )
                ORDER BY r.updated_at, r.id
                """,
                (cutoff,),
            ).fetchall()

    def mark_resources_expired(self, resource_ids: Iterable[int]) -> int:
        self.initialize()
        ids = list(dict.fromkeys(int(resource_id) for resource_id in resource_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE resources
                SET download_status = 'expired',
                    path = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [self.clock(), *ids],
            )
        return int(cursor.rowcount)

    def approval_feedback_retention_candidates(
        self,
        *,
        content_cutoff: str,
        metadata_cutoff: str | None,
    ) -> dict[str, int]:
        self.initialize()
        content_query = """
            SELECT COUNT(*) AS count
            FROM approval_feedback
            WHERE content_expired_at IS NULL
              AND julianday(created_at) <= julianday(?)
        """
        content_params: list[Any] = [content_cutoff]
        if metadata_cutoff is not None:
            content_query += " AND julianday(created_at) > julianday(?)"
            content_params.append(metadata_cutoff)
        with self.connect() as conn:
            content = conn.execute(content_query, content_params).fetchone()
            metadata_count = 0
            if metadata_cutoff is not None:
                metadata = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM approval_feedback
                    WHERE julianday(created_at) <= julianday(?)
                    """,
                    (metadata_cutoff,),
                ).fetchone()
                metadata_count = int(metadata["count"])
        return {"content": int(content["count"]), "metadata": metadata_count}

    def prune_approval_feedback(
        self,
        *,
        content_cutoff: str,
        metadata_cutoff: str | None,
        expired_at: str,
    ) -> tuple[int, int]:
        self.initialize()
        with self.connect() as conn:
            metadata_deleted = 0
            if metadata_cutoff is not None:
                deleted = conn.execute(
                    """
                    DELETE FROM approval_feedback
                    WHERE julianday(created_at) <= julianday(?)
                    """,
                    (metadata_cutoff,),
                )
                metadata_deleted = int(deleted.rowcount)
            expired = conn.execute(
                """
                UPDATE approval_feedback
                SET suggested_reply = NULL,
                    final_reply = NULL,
                    note = NULL,
                    content_expired_at = ?
                WHERE content_expired_at IS NULL
                  AND julianday(created_at) <= julianday(?)
                """,
                (expired_at, content_cutoff),
            )
        return int(expired.rowcount), metadata_deleted

    def create_task_for_message(
        self,
        message: NormalizedMessage,
        *,
        watch_until: str,
        task_label: str | None = None,
        agent_working_dir: str | None = None,
    ) -> TaskRecord:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            task_id = self._create_task_for_message(
                conn,
                message,
                watch_until=watch_until,
                task_label=task_label,
                agent_working_dir=agent_working_dir,
                now=now,
            )
        return self.get_task_by_id(task_id)

    def create_task_for_message_and_audit(
        self,
        message: NormalizedMessage,
        *,
        watch_until: str,
        reason: str = "new_trigger",
        candidates_count: int = 0,
        router_called: bool = False,
        matched_by: str = "new_trigger",
        agent_working_dir: str | None = None,
    ) -> tuple[TaskRecord, RouteDecision]:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            task_id = self._create_task_for_message(
                conn,
                message,
                watch_until=watch_until,
                task_label=None,
                agent_working_dir=agent_working_dir,
                now=now,
            )
            task = self._get_task_by_id(conn, task_id)
            decision = RouteDecision(
                "new_task",
                target_task_id=task.id,
                target_task_short_id=task.short_id,
                reason=reason,
                candidates_count=candidates_count,
                shortcut_hit=False,
                router_called=router_called,
                matched_by=matched_by,
            )
            self._record_routing_audit(
                conn, message_id=message.message_id, decision=decision
            )
        return task, decision

    def attach_message_to_task(
        self, task_id: int, message: NormalizedMessage, *, watch_until: str
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            self._attach_message_to_task(
                conn, task_id, message, watch_until=watch_until, now=now
            )

    def attach_message_to_task_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
        *,
        watch_until: str,
        candidates_count: int,
        matched_by: str,
        reason: str = "deterministic_shortcut",
    ) -> RouteDecision:
        self.initialize()
        now = self.clock()
        decision = RouteDecision(
            "attach_task",
            target_task_id=task.id,
            target_task_short_id=task.short_id,
            reason=reason,
            candidates_count=candidates_count,
            shortcut_hit=True,
            matched_by=matched_by,
        )
        with self.connect() as conn:
            self._attach_message_to_task(
                conn, task.id, message, watch_until=watch_until, now=now
            )
            self._record_routing_audit(
                conn, message_id=message.message_id, decision=decision
            )
        return decision

    def close_task_for_owner_takeover(self, task_id: int) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            self._close_task_for_owner_takeover(conn, task_id, now=now)

    def close_task_for_owner_escalation(
        self,
        *,
        task_id: int,
        payload: dict[str, Any],
        task_label: str | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> int | None:
        """End automated ownership and atomically queue the manual handoff."""
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if task["status"] != TaskStatus.WATCHING.value:
                return None
            pending_rows = conn.execute(
                "SELECT id FROM approvals WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            ).fetchall()
            pending_approval_ids = [int(row["id"]) for row in pending_rows]
            task_cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, task_label = COALESCE(?, task_label), updated_at = ?,
                    closed_at = ?, watch_until = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.CLOSED.value,
                    None if task_label is None else _truncate(task_label, limit=100),
                    now,
                    now,
                    task_id,
                    TaskStatus.WATCHING.value,
                ),
            )
            if task_cursor.rowcount != 1:
                return None
            conn.execute(
                """
                UPDATE actions
                SET status = ?, updated_at = ?
                WHERE task_id = ? AND kind = ? AND status IN ('pending', 'sending')
                """,
                (
                    ActionStatus.CANCELLED.value,
                    now,
                    task_id,
                    ActionKind.SEND_REPLY.value,
                ),
            )
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (ApprovalStatus.EXPIRED.value, now, task_id),
            )
            self._cancel_pending_actions_for_approvals_locked(
                conn,
                approval_ids=pending_approval_ids,
                now=now,
            )
            return self._create_owner_notification_action_locked(
                conn,
                task_id=task_id,
                payload=payload,
                execution_mode=execution_mode,
                now=now,
            )

    def close_task_by_operator(self, task_id: int | str) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            task = self._get_task_by_lookup(conn, task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if task.status != TaskStatus.WATCHING.value:
                return {
                    "changed": False,
                    "task": _task_command_summary(task),
                    "previous_status": task.status,
                    "expired_approvals": 0,
                    "cancelled_actions": 0,
                }
            expired_approvals, cancelled_actions = self._close_task_by_operator_locked(
                conn, task.id, now=now
            )
            updated = self._get_task_by_id(conn, task.id)
        return {
            "changed": True,
            "task": _task_command_summary(updated),
            "previous_status": task.status,
            "expired_approvals": expired_approvals,
            "cancelled_actions": cancelled_actions,
        }

    def reopen_task_by_operator(
        self, task_id: int | str, *, watch_until: str
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            task = self._get_task_by_lookup(conn, task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            if task.status == TaskStatus.WATCHING.value:
                return {
                    "changed": False,
                    "task": _task_command_summary(task),
                    "previous_status": task.status,
                }
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, closed_at = NULL, watch_until = ?
                WHERE id = ?
                """,
                (TaskStatus.WATCHING.value, now, watch_until, task.id),
            )
            updated = self._get_task_by_id(conn, task.id)
        return {
            "changed": True,
            "task": _task_command_summary(updated),
            "previous_status": task.status,
        }

    def close_task_for_owner_takeover_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
    ) -> RouteDecision:
        self.initialize()
        now = self.clock()
        decision = RouteDecision(
            "human_taken_over",
            target_task_id=task.id,
            target_task_short_id=task.short_id,
            reason="owner_message_related_to_active_task",
            candidates_count=1,
            matched_by="owner_takeover",
        )
        with self.connect() as conn:
            self._close_task_for_owner_takeover(conn, task.id, now=now)
            self._record_routing_audit(
                conn, message_id=message.message_id, decision=decision
            )
        return decision

    def get_task_by_id(self, task_id: int) -> TaskRecord:
        self.initialize()
        with self.connect() as conn:
            return self._get_task_by_id(conn, task_id)

    def get_task_by_short_id(self, short_id: str) -> TaskRecord | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE short_id = ?",
                (short_id,),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def get_active_tasks_for_chat(self, chat_id: str, *, now: str) -> list[TaskRecord]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE chat_id = ?
                  AND status = 'watching'
                  AND (watch_until IS NULL OR julianday(watch_until) > julianday(?))
                ORDER BY updated_at DESC, id DESC
                """,
                (chat_id, now),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_active_tasks_by_watch_key(
        self,
        chat_id: str,
        key: str,
        *,
        now: str,
    ) -> list[TaskRecord]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*
                FROM tasks t
                JOIN task_watch_keys wk ON wk.task_id = t.id
                WHERE t.chat_id = ?
                  AND wk.key = ?
                  AND t.status = 'watching'
                  AND (t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))
                ORDER BY t.updated_at DESC, t.id DESC
                """,
                (chat_id, key, now),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_latest_task_sender_message_sent_at(
        self,
        task_id: int,
        sender_id: str,
        *,
        exclude_message_id: str | None = None,
    ) -> str | None:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.message_id, m.sent_at
                FROM task_messages tm
                JOIN messages m ON m.message_id = tm.message_id
                WHERE tm.task_id = ?
                  AND m.sender_id = ?
                  AND m.sent_at IS NOT NULL
                  AND (? IS NULL OR m.message_id != ?)
                """,
                (task_id, sender_id, exclude_message_id, exclude_message_id),
            ).fetchall()
        latest_value: str | None = None
        latest_dt: datetime | None = None
        for row in rows:
            sent_at = row["sent_at"]
            parsed = _parse_datetime_or_none(sent_at)
            if parsed is None:
                continue
            if latest_dt is None or parsed > latest_dt:
                latest_dt = parsed
                latest_value = sent_at
        return latest_value

    def get_recent_closed_tasks(
        self, chat_id: str, *, limit: int = 20
    ) -> list[TaskRecord]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE chat_id = ?
                  AND status != 'watching'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_related_closed_tasks(
        self,
        message: NormalizedMessage,
        *,
        since: str,
        limit: int = 20,
    ) -> list[TaskRecord]:
        if not message.chat_id:
            return []
        conditions: list[str] = []
        params: list[Any] = [message.chat_id, since]
        if message.sender_id:
            conditions.append(
                """
                EXISTS (
                  SELECT 1 FROM task_watch_keys wk
                  WHERE wk.task_id = t.id AND wk.key = ?
                )
                """
            )
            params.append(f"user:{message.sender_id}")
        if message.thread_id:
            conditions.append("t.thread_id = ?")
            params.append(message.thread_id)
            conditions.append(
                """
                EXISTS (
                  SELECT 1 FROM task_watch_keys wk
                  WHERE wk.task_id = t.id AND wk.key = ?
                )
                """
            )
            params.append(f"thread:{message.thread_id}")
        if message.reply_to_message_id:
            conditions.append(
                """
                EXISTS (
                  SELECT 1 FROM task_messages tm
                  WHERE tm.task_id = t.id AND tm.message_id = ?
                )
                """
            )
            params.append(message.reply_to_message_id)
            conditions.append(
                """
                EXISTS (
                  SELECT 1 FROM task_watch_keys wk
                  WHERE wk.task_id = t.id AND wk.key = ?
                )
                """
            )
            params.append(f"msg:{message.reply_to_message_id}")
        for pattern in _closed_recall_text_patterns(message.text):
            conditions.append(
                """
                (
                  lower(coalesce(t.task_label, '')) LIKE ?
                  OR lower(coalesce(t.last_user_message, '')) LIKE ?
                  OR lower(coalesce(t.last_agent_reply, '')) LIKE ?
                )
                """
            )
            params.extend([pattern, pattern, pattern])
        if not conditions:
            return []
        where_related = " OR ".join(f"({condition})" for condition in conditions)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tasks t
                WHERE t.chat_id = ?
                  AND t.status != 'watching'
                  AND julianday(t.updated_at) >= julianday(?)
                  AND ({where_related})
                ORDER BY t.updated_at DESC, t.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def find_task_ids_for_message(self, message_id: str) -> list[int]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM task_messages WHERE message_id = ? ORDER BY task_id",
                (message_id,),
            ).fetchall()
        return [int(row["task_id"]) for row in rows]

    def find_task_for_sent_action_message(self, message_id: str) -> TaskRecord | None:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, a.result_json
                FROM actions a
                JOIN tasks t ON t.id = a.task_id
                WHERE a.kind = ? AND a.result_json IS NOT NULL
                  AND a.result_json LIKE ?
                ORDER BY a.updated_at DESC, a.id DESC
                """,
                ("send_reply", f"%{message_id}%"),
            ).fetchall()
        matches: dict[int, TaskRecord] = {}
        for row in rows:
            if _action_result_refs_message(row["result_json"], message_id):
                task = _task_from_row(row)
                matches[task.id] = task
        if len(matches) == 1:
            return next(iter(matches.values()))
        return None

    def record_agent_message_for_task(
        self,
        task_id: int,
        message: NormalizedMessage,
        *,
        watch_until: str,
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            self._record_agent_message_for_task(
                conn, task_id, message, watch_until=watch_until, now=now
            )

    def record_agent_message_for_task_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
        *,
        watch_until: str,
    ) -> RouteDecision:
        self.initialize()
        now = self.clock()
        decision = RouteDecision(
            "ignore",
            target_task_id=task.id,
            target_task_short_id=task.short_id,
            reason="self_message",
            matched_by="sent_action",
        )
        with self.connect() as conn:
            self._record_agent_message_for_task(
                conn,
                task.id,
                message,
                watch_until=watch_until,
                now=now,
            )
            self._record_routing_audit(
                conn, message_id=message.message_id, decision=decision
            )
        return decision

    def list_active_watch_targets(self, *, now: str) -> list[dict[str, str | None]]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT
                  t.chat_id AS chat_id,
                  t.chat_type AS chat_type,
                  substr(wk.key, ?) AS thread_id
                FROM tasks t
                JOIN task_watch_keys wk ON wk.task_id = t.id
                WHERE t.status = 'watching'
                  AND (t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))
                  AND t.chat_id IS NOT NULL
                  AND wk.key LIKE 'thread:%'
                  AND length(wk.key) > ?
                UNION
                SELECT DISTINCT
                  t.chat_id AS chat_id,
                  t.chat_type AS chat_type,
                  NULL AS thread_id
                FROM tasks t
                WHERE t.status = 'watching'
                  AND (t.watch_until IS NULL OR julianday(t.watch_until) > julianday(?))
                  AND t.chat_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM task_watch_keys wk
                    WHERE wk.task_id = t.id
                      AND wk.key LIKE 'thread:%'
                  )
                ORDER BY chat_id, thread_id
                """,
                (len("thread:") + 1, now, len("thread:"), now),
            ).fetchall()
        return [
            {
                "chat_id": row["chat_id"],
                "chat_type": row["chat_type"],
                "thread_id": row["thread_id"],
            }
            for row in rows
        ]

    def record_routing_audit(self, *, message_id: str, decision: RouteDecision) -> None:
        self.initialize()
        with self.connect() as conn:
            self._record_routing_audit(conn, message_id=message_id, decision=decision)

    def add_task_watch_keys(self, task_id: int, keys: Iterable[str]) -> None:
        self.initialize()
        unique_keys = sorted(set(keys))
        if not unique_keys:
            return
        with self.connect() as conn:
            self._add_watch_keys(conn, task_id, unique_keys, self.clock())

    def has_resource_eligible_routing_audit(self, message_id: str) -> bool:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM routing_audits
                WHERE message_id = ?
                  AND route IN ('new_task', 'attach_task', 'reopen_task', 'ambiguous')
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return row is not None

    def has_missing_resources(self, resources: Iterable[ResourceRef]) -> bool:
        self.initialize()
        refs = list(resources)
        if not refs:
            return False
        with self.connect() as conn:
            for resource in refs:
                row = conn.execute(
                    """
                    SELECT 1 FROM resources
                    WHERE message_id = ? AND file_key = ? AND resource_type = ?
                    LIMIT 1
                    """,
                    (resource.message_id, resource.file_key, resource.resource_type),
                ).fetchone()
                if row is None:
                    return True
        return False

    def count_pending_actions(self) -> int:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM actions WHERE status IN ('pending', 'sending')"
            ).fetchone()
        return int(row["count"])

    def list_dispatchable_actions(
        self, *, limit: int = 50, kind: str | None = None
    ) -> list[ActionRecord]:
        self.initialize()
        with self.connect() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM actions
                    WHERE status = 'pending'
                      AND kind IN ('send_reply', 'owner_notification')
                    ORDER BY CASE WHEN execution_mode = 'production' THEN 0 ELSE 1 END,
                             created_at, id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM actions
                    WHERE status = 'pending'
                      AND kind = ?
                    ORDER BY CASE WHEN execution_mode = 'production' THEN 0 ELSE 1 END,
                             created_at, id
                    LIMIT ?
                    """,
                    (kind, limit),
                ).fetchall()
        return [_action_from_row(row) for row in rows]

    def get_action(self, action_id: int) -> ActionRecord | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return None if row is None else _action_from_row(row)

    def claim_action_for_dispatch(
        self, action_id: int, *, run_id: str | None = None
    ) -> DispatchClaim | None:
        self.initialize()
        now = self.clock()
        claim_token = new_run_id("claim")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE actions
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                ("sending", now, action_id, "pending"),
            )
            if cursor.rowcount != 1:
                return None
            attempt_cursor = conn.execute(
                """
                INSERT INTO dispatch_attempts(action_id, run_id, claim_token, status, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    run_id,
                    claim_token,
                    DispatchAttemptStatus.STARTED.value,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE id = ?",
                (int(attempt_cursor.lastrowid),),
            ).fetchone()
        if row is None or attempt is None:
            return None
        return DispatchClaim(
            action=_action_from_row(row), attempt=_dispatch_attempt_from_row(attempt)
        )

    def list_dispatch_attempts(self, action_id: int) -> list[DispatchAttemptRecord]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM dispatch_attempts
                WHERE action_id = ?
                ORDER BY started_at, id
                """,
                (action_id,),
            ).fetchall()
        return [_dispatch_attempt_from_row(row) for row in rows]

    def update_dispatch_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        dry_run_result: dict[str, Any] | None = None,
        send_result: dict[str, Any] | None = None,
        readback_result: dict[str, Any] | None = None,
        sent_message_id: str | None = None,
        error_stage: str | None = None,
        finish: bool = False,
    ) -> DispatchAttemptRecord:
        self.initialize()
        assignments = ["status = ?"]
        params: list[Any] = [status]
        if dry_run_result is not None:
            assignments.append("dry_run_result_json = ?")
            params.append(json.dumps(dry_run_result, ensure_ascii=False, default=str))
        if send_result is not None:
            assignments.append("send_result_json = ?")
            params.append(json.dumps(send_result, ensure_ascii=False, default=str))
        if readback_result is not None:
            assignments.append("readback_result_json = ?")
            params.append(json.dumps(readback_result, ensure_ascii=False, default=str))
        if sent_message_id is not None:
            assignments.append("sent_message_id = ?")
            params.append(sent_message_id)
        if error_stage is not None:
            assignments.append("error_stage = ?")
            params.append(error_stage)
        if finish or status in {
            DispatchAttemptStatus.READBACK_OK.value,
            DispatchAttemptStatus.FAILED.value,
            DispatchAttemptStatus.UNCERTAIN.value,
        }:
            assignments.append("finished_at = ?")
            params.append(self.clock())
        params.append(attempt_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE dispatch_attempts SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"dispatch attempt not found: {attempt_id}")
        return _dispatch_attempt_from_row(row)

    def get_dispatch_inspection(self, action_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            action = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
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
        return {
            "action": _action_record_dict(_action_from_row(action)),
            "attempts": [
                _dispatch_attempt_dict(_dispatch_attempt_from_row(row))
                for row in attempts
            ],
        }

    def find_stale_sending_actions(
        self, *, stale_after_seconds: int = 900, now: str | None = None
    ) -> list[ActionRecord]:
        self.initialize()
        cutoff = _minus_seconds(now or self.clock(), stale_after_seconds)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM actions
                WHERE status = 'sending'
                  AND julianday(updated_at) <= julianday(?)
                ORDER BY updated_at, id
                """,
                (cutoff,),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def mark_stale_sending_actions_failed_needs_review(
        self,
        *,
        stale_after_seconds: int = 900,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        effective_now = now or self.clock()
        cutoff = _minus_seconds(effective_now, stale_after_seconds)
        recovered: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM actions
                WHERE status = 'sending'
                  AND julianday(updated_at) <= julianday(?)
                ORDER BY updated_at, id
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                action_id = int(row["id"])
                latest = _latest_dispatch_attempt_locked(conn, action_id=action_id)
                if _attempt_proves_readback(latest):
                    result = _stale_sent_result(row, latest)
                    conn.execute(
                        """
                        UPDATE actions
                        SET status = ?, result_json = ?, updated_at = ?
                        WHERE id = ? AND status = 'sending'
                        """,
                        (
                            ActionStatus.SENT.value,
                            json.dumps(result, ensure_ascii=False, default=str),
                            effective_now,
                            action_id,
                        ),
                    )
                    if latest["finished_at"] is None:
                        conn.execute(
                            "UPDATE dispatch_attempts SET finished_at = ? WHERE id = ?",
                            (effective_now, latest["id"]),
                        )
                    recovered.append(
                        {"action_id": action_id, "status": ActionStatus.SENT.value}
                    )
                    continue

                result = _stale_needs_review_result(row, latest)
                conn.execute(
                    """
                    UPDATE actions
                    SET status = ?, result_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'sending'
                    """,
                    (
                        ActionStatus.FAILED_NEEDS_REVIEW.value,
                        json.dumps(result, ensure_ascii=False, default=str),
                        effective_now,
                        action_id,
                    ),
                )
                if latest is None:
                    conn.execute(
                        """
                        INSERT INTO dispatch_attempts(
                          action_id, run_id, claim_token, status, error_stage, started_at, finished_at
                        ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            action_id,
                            new_run_id("claim"),
                            DispatchAttemptStatus.UNCERTAIN.value,
                            DispatchErrorStage.RECOVERY.value,
                            effective_now,
                            effective_now,
                        ),
                    )
                elif latest["finished_at"] is None:
                    conn.execute(
                        """
                        UPDATE dispatch_attempts
                        SET status = ?, error_stage = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (
                            DispatchAttemptStatus.UNCERTAIN.value,
                            DispatchErrorStage.RECOVERY.value,
                            effective_now,
                            latest["id"],
                        ),
                    )
                recovered.append(
                    {
                        "action_id": action_id,
                        "status": ActionStatus.FAILED_NEEDS_REVIEW.value,
                    }
                )
        return recovered

    def retry_dispatch_action(self, action_id: int) -> ActionRecord:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"action not found: {action_id}")
            if row["status"] not in {
                ActionStatus.FAILED.value,
                ActionStatus.FAILED_NEEDS_REVIEW.value,
            }:
                raise ValueError(
                    "dispatch retry only accepts failed or failed_needs_review actions"
                )
            if (
                row["kind"] == ActionKind.SEND_REPLY.value
                and row["task_id"] is not None
                and row["target_message_id"] is not None
                and _has_active_send_reply_action(
                    conn,
                    task_id=int(row["task_id"]),
                    target_message_id=row["target_message_id"],
                    exclude_action_id=action_id,
                    execution_mode=row["execution_mode"],
                )
            ):
                raise ValueError(
                    "active send action already exists for this task and reply target"
                )
            conn.execute(
                """
                UPDATE actions
                SET status = ?, dry_run = ?, result_json = NULL, updated_at = ?
                WHERE id = ?
                """,
                (ActionStatus.PENDING.value, 1, now, action_id),
            )
            updated = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        if updated is None:
            raise ValueError(f"action not found: {action_id}")
        return _action_from_row(updated)

    def cancel_dispatch_action(self, action_id: int) -> ActionRecord:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"action not found: {action_id}")
            if row["status"] == ActionStatus.SENT.value:
                raise ValueError("sent actions cannot be cancelled")
            if row["status"] != ActionStatus.CANCELLED.value:
                conn.execute(
                    """
                    UPDATE actions
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ActionStatus.CANCELLED.value, now, action_id),
                )
            updated = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        if updated is None:
            raise ValueError(f"action not found: {action_id}")
        return _action_from_row(updated)

    def mark_action_sent_after_evidence(
        self,
        action_id: int,
        *,
        sent_message_id: str,
        result: dict[str, Any],
        run_id: str | None = None,
        readback_message: NormalizedMessage | None = None,
        watch_until: str | None = None,
    ) -> ActionRecord:
        self.initialize()
        if not sent_message_id:
            raise ValueError("sent_message_id is required")
        readback = result.get("readback")
        if not isinstance(readback, dict) or readback.get("ok") is not True:
            raise ValueError("readback evidence is required before marking sent")
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"action not found: {action_id}")
            if row["status"] == ActionStatus.CANCELLED.value:
                raise ValueError("cancelled actions cannot be marked sent")
            result_with_id = dict(result)
            result_with_id["sent_message_id"] = sent_message_id
            if readback_message is not None:
                readback_result = dict(readback)
                readback_result["inserted"] = self._upsert_message_locked(
                    conn, readback_message, now=now
                )
                result_with_id["readback"] = readback_result
                readback = readback_result
                if (
                    row["kind"] == ActionKind.SEND_REPLY.value
                    and row["task_id"] is not None
                    and watch_until is not None
                ):
                    self._record_agent_message_for_task(
                        conn,
                        int(row["task_id"]),
                        readback_message,
                        watch_until=watch_until,
                        now=now,
                    )
            latest = _latest_dispatch_attempt_locked(conn, action_id=action_id)
            if latest is None:
                conn.execute(
                    """
                    INSERT INTO dispatch_attempts(
                      action_id, run_id, claim_token, status, readback_result_json,
                      sent_message_id, error_stage, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        run_id,
                        new_run_id("claim"),
                        DispatchAttemptStatus.READBACK_OK.value,
                        json.dumps(readback, ensure_ascii=False, default=str),
                        sent_message_id,
                        DispatchErrorStage.RECOVERY.value,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE dispatch_attempts
                    SET status = ?, readback_result_json = ?, sent_message_id = ?, error_stage = NULL, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        DispatchAttemptStatus.READBACK_OK.value,
                        json.dumps(readback, ensure_ascii=False, default=str),
                        sent_message_id,
                        now,
                        latest["id"],
                    ),
                )
            conn.execute(
                """
                UPDATE actions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ActionStatus.SENT.value,
                    json.dumps(result_with_id, ensure_ascii=False, default=str),
                    now,
                    action_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        if updated is None:
            raise ValueError(f"action not found: {action_id}")
        return _action_from_row(updated)

    def record_action_preview(self, action_id: int, result: dict[str, Any]) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE actions
                SET result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    json.dumps(result, ensure_ascii=False, default=str),
                    self.clock(),
                    action_id,
                ),
            )

    def finish_action(
        self,
        action_id: int,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE actions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False, default=str),
                    self.clock(),
                    action_id,
                ),
            )

    def finish_claimed_action(
        self,
        action_id: int,
        *,
        attempt_id: int,
        status: str,
        result: dict[str, Any],
    ) -> ActionRecord | None:
        self.initialize()
        with self.connect() as conn:
            latest = _latest_dispatch_attempt_locked(conn, action_id=action_id)
            if latest is None or int(latest["id"]) != attempt_id:
                return None
            cursor = conn.execute(
                """
                UPDATE actions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False, default=str),
                    self.clock(),
                    action_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return None if row is None else _action_from_row(row)

    def expire_pending_approvals(self, *, now: str | None = None) -> int:
        self.initialize()
        effective_now = now or self.clock()
        with self.connect() as conn:
            return self._expire_pending_approvals_locked(conn, now=effective_now)

    def replay_summary(
        self, message_id: str, *, now: str | None = None
    ) -> dict[str, Any] | None:
        self.initialize()
        effective_now = now or self.clock()
        with self.connect() as conn:
            message = conn.execute(
                """
                SELECT message_id, chat_id, chat_type, sender_id, sender_role, sent_at, text
                FROM messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if message is None:
                return None
            audits = conn.execute(
                """
                SELECT route, route_reason, candidates_count, shortcut_hit, router_called, matched_by,
                       target_task_id, created_at
                FROM routing_audits
                WHERE message_id = ?
                ORDER BY created_at, id
                """,
                (message_id,),
            ).fetchall()
            task_ids = self.find_task_ids_for_message(message_id)
            actions = conn.execute(
                """
                SELECT id, kind, status, task_id, target_message_id, payload_json, result_json, updated_at
                FROM actions
                WHERE target_message_id = ?
                   OR task_id IN (
                        SELECT task_id FROM task_messages WHERE message_id = ?
                   )
                ORDER BY created_at, id
                """,
                (message_id, message_id),
            ).fetchall()
            approvals = conn.execute(
                """
                SELECT id, short_id, task_id, kind, status, preview, payload_json,
                       created_at, expires_at, resolved_at
                FROM approvals
                WHERE task_id IN (
                    SELECT task_id FROM task_messages WHERE message_id = ?
                )
                ORDER BY created_at, id
                """,
                (message_id,),
            ).fetchall()
        return {
            "message": _row_dict(message),
            "routing_audits": [_row_dict(row) for row in audits],
            "task_ids": task_ids,
            "approvals": [
                _approval_read_model(
                    row, now=effective_now, json_columns=("payload_json",)
                )
                for row in approvals
            ],
            "actions": [
                _json_row_dict(row, "payload_json", "result_json") for row in actions
            ],
        }

    def list_task_message_ids(self, task_id: int) -> list[str]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT tm.message_id
                FROM task_messages tm
                JOIN messages m ON m.message_id = tm.message_id
                WHERE tm.task_id = ?
                ORDER BY COALESCE(m.sent_at, tm.created_at), tm.created_at, tm.message_id
                """,
                (task_id,),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def count_task_messages_by_task_ids(
        self, task_ids: Iterable[int]
    ) -> dict[int, int]:
        ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
        if not ids:
            return {}
        self.initialize()
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task_id, COUNT(*) AS message_count
                FROM task_messages
                WHERE task_id IN ({placeholders})
                GROUP BY task_id
                """,
                ids,
            ).fetchall()
        counts = {int(row["task_id"]): int(row["message_count"]) for row in rows}
        return {task_id: counts.get(task_id, 0) for task_id in ids}

    def list_recent_task_context(
        self, task_ids: Iterable[int], *, messages_per_task: int = 5
    ) -> dict[int, dict[str, Any]]:
        ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
        if not ids:
            return {}
        limit = max(0, int(messages_per_task))
        self.initialize()
        contexts: dict[int, dict[str, Any]] = {}
        with self.connect() as conn:
            for task_id in ids:
                count_row = conn.execute(
                    "SELECT COUNT(*) AS message_count FROM task_messages WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                message_count = (
                    0 if count_row is None else int(count_row["message_count"])
                )
                rows = conn.execute(
                    """
                    SELECT tm.message_id, tm.role, tm.created_at AS task_message_created_at,
                           m.chat_id, m.chat_type, m.sender_id,
                           m.sender_name, m.sender_role, m.sent_at, m.thread_id,
                           m.reply_to_message_id, m.text
                    FROM task_messages tm
                    LEFT JOIN messages m ON m.message_id = tm.message_id
                    WHERE tm.task_id = ?
                    ORDER BY tm.created_at DESC, tm.message_id DESC
                    LIMIT ?
                    """,
                    (task_id, limit),
                ).fetchall()
                contexts[task_id] = {
                    "message_count": message_count,
                    "truncated": message_count > len(rows),
                    "recent_messages": [
                        _task_context_message(row) for row in reversed(rows)
                    ],
                }
        return {
            task_id: contexts.get(
                task_id,
                {"message_count": 0, "truncated": False, "recent_messages": []},
            )
            for task_id in ids
        }

    def list_resources_for_messages(
        self, message_ids: Iterable[str]
    ) -> list[sqlite3.Row]:
        self.initialize()
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM resources
                WHERE message_id IN ({placeholders})
                ORDER BY message_id, id
                """,
                ids,
            ).fetchall()

    def get_initialized_agent_session_id(
        self, task_id: int, *, backend_provider: str
    ) -> str | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT agent_session_id, agent_session_provider FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        session_id = row["agent_session_id"]
        if not session_id:
            return None
        if row["agent_session_provider"] != backend_provider:
            return None
        return str(session_id)

    def set_task_agent_session_id(
        self, task_id: int, session_id: str, *, backend_provider: str
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET agent_session_id = ?, agent_session_provider = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_id, backend_provider, self.clock(), task_id),
            )

    def update_task_after_agent(
        self,
        *,
        task_id: int,
        task_label: str | None = None,
        status: str | None = None,
        watch_until: str | None = None,
    ) -> None:
        self.initialize()
        assignments = ["updated_at = ?"]
        params: list[Any] = [self.clock()]
        if task_label is not None:
            assignments.append("task_label = ?")
            params.append(_truncate(task_label, limit=100))
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
            if not LifecycleStatePolicy.task_status_closes_at(status):
                assignments.append("closed_at = NULL")
            else:
                assignments.append("closed_at = ?")
                params.append(self.clock())
        if watch_until is not None:
            assignments.append("watch_until = ?")
            params.append(watch_until)
        params.append(task_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def record_agent_audit(
        self,
        *,
        backend_provider: str,
        request_type: str,
        task_id: int | None,
        agent_session_id: str | None,
        input_message_ids: Iterable[str],
        input_resource_ids: Iterable[str],
        response: dict[str, Any] | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
        prompt: dict[str, Any] | None = None,
        tool_permissions_profile: str | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_audits(
                  backend_provider, request_type, task_id, agent_session_id, input_message_ids_json,
                  input_resource_ids_json, response_json, error, latency_ms, prompt_json,
                  tool_permissions_profile, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    backend_provider,
                    request_type,
                    task_id,
                    agent_session_id,
                    json.dumps(
                        list(input_message_ids), ensure_ascii=False, default=str
                    ),
                    json.dumps(
                        list(input_resource_ids), ensure_ascii=False, default=str
                    ),
                    None
                    if response is None
                    else json.dumps(response, ensure_ascii=False, default=str),
                    error,
                    latency_ms,
                    None
                    if prompt is None
                    else json.dumps(prompt, ensure_ascii=False, default=str),
                    tool_permissions_profile,
                    self.clock(),
                ),
            )

    def create_send_reply_action(
        self,
        *,
        task_id: int,
        target_message_id: str,
        payload: dict[str, Any],
        approval_id: int | None = None,
        execution_mode: ExecutionMode = "production",
    ) -> int | None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            return self._create_send_reply_action_locked(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                execution_mode=execution_mode,
                now=now,
            )

    def create_owner_notification_action(
        self,
        *,
        task_id: int | None,
        payload: dict[str, Any],
        execution_mode: ExecutionMode = "production",
    ) -> int:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            return self._create_owner_notification_action_locked(
                conn,
                task_id=task_id,
                payload=payload,
                execution_mode=execution_mode,
                now=now,
            )

    def create_send_reply_approval(
        self,
        *,
        task_id: int,
        preview: str,
        payload: dict[str, Any],
        notify_payload: dict[str, Any] | None = None,
        approval_timeout_hours: int | None = 24,
        execution_mode: ExecutionMode = "production",
    ) -> int:
        self.initialize()
        now = self.clock()
        expires_at = (
            None
            if approval_timeout_hours is None
            else _plus_hours(now, approval_timeout_hours)
        )
        with self.connect() as conn:
            short_id = self._unique_short_id_in_table(
                conn, "approvals", "a", f"{task_id}:{preview}:{now}"
            )
            cursor = conn.execute(
                """
                INSERT INTO approvals(short_id, task_id, kind, status, payload_json, preview, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    short_id,
                    task_id,
                    ApprovalKind.SEND_REPLY.value,
                    ApprovalStatus.PENDING.value,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    preview,
                    now,
                    expires_at,
                ),
            )
            approval_id = int(cursor.lastrowid)
            if notify_payload is not None:
                self._create_owner_notification_action_locked(
                    conn,
                    task_id=task_id,
                    approval_id=approval_id,
                    payload=_approval_notification_payload(
                        notify_payload,
                        approval_short_id=short_id,
                        approval_payload=payload,
                    ),
                    execution_mode=execution_mode,
                    now=now,
                )
        return approval_id

    def apply_approval_command(
        self,
        *,
        message_id: str,
        command: str,
        verb: str,
        target_id: str,
        final_reply: str | None = None,
        keep_watching_until: str | None = None,
        actor: str = "owner",
        feedback_reason: FeedbackReason | None = None,
        note: str | None = None,
        execution_mode: ExecutionMode = "production",
        requested_outcome: ApprovalOutcome | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            keep_watching_reject_task_id = self._keep_watching_reject_task_id_locked(
                conn,
                verb=verb,
                target_id=target_id,
            )
            self._expire_pending_approvals_locked(
                conn,
                now=now,
                exclude_task_id=keep_watching_reject_task_id,
            )
            existing = conn.execute(
                "SELECT status, result_json FROM approval_commands WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "status": "duplicate",
                    "result": json.loads(existing["result_json"] or "{}"),
                }

            conn.execute("SAVEPOINT approval_command")
            try:
                result = self._apply_approval_command_locked(
                    conn,
                    verb=verb,
                    target_id=target_id,
                    final_reply=final_reply,
                    now=now,
                    keep_watching_until=keep_watching_until,
                    command_id=message_id,
                    actor=actor,
                    feedback_reason=feedback_reason,
                    note=note,
                    execution_mode=execution_mode,
                    requested_outcome=requested_outcome,
                )
            except Exception as exc:
                # State changes are rolled back, but the command itself is still
                # recorded below so status/replay can explain failed approvals.
                conn.execute("ROLLBACK TO SAVEPOINT approval_command")
                conn.execute("RELEASE SAVEPOINT approval_command")
                result = {"error": str(exc)}
                status = "failed"
            else:
                conn.execute("RELEASE SAVEPOINT approval_command")
                status = str(result.pop("_status", "applied"))
            conn.execute(
                """
                INSERT INTO approval_commands(message_id, command, status, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    command,
                    status,
                    json.dumps(result, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
        return {"status": status, "result": result}

    def _expire_pending_approvals_locked(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        exclude_task_id: int | None = None,
    ) -> int:
        query = """
            SELECT id
            FROM approvals
            WHERE status = 'pending'
              AND expires_at IS NOT NULL
              AND julianday(expires_at) < julianday(?)
        """
        params: list[Any] = [now]
        if exclude_task_id is not None:
            query += " AND (task_id IS NULL OR task_id != ?)"
            params.append(exclude_task_id)
        query += " ORDER BY expires_at, id"
        rows = conn.execute(query, params).fetchall()
        expired = 0
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (ApprovalStatus.EXPIRED.value, now, row["id"]),
            )
            if cursor.rowcount != 1:
                continue
            expired += 1
            self._cancel_pending_actions_for_approvals_locked(
                conn, approval_ids=[int(row["id"])], now=now
            )
        return expired

    def _apply_approval_command_locked(
        self,
        conn: sqlite3.Connection,
        *,
        verb: str,
        target_id: str,
        final_reply: str | None,
        now: str,
        keep_watching_until: str | None,
        command_id: str,
        actor: str,
        feedback_reason: FeedbackReason | None,
        note: str | None,
        execution_mode: ExecutionMode,
        requested_outcome: ApprovalOutcome | None,
    ) -> dict[str, Any]:
        if execution_mode not in {"dry_run", "production"}:
            raise ValueError(
                "approval commands require dry_run or production execution_mode"
            )
        if not actor.strip():
            raise ValueError("approval command actor is required")
        if feedback_reason not in {
            None,
            "inaccurate_or_unsupported",
            "incomplete_context",
            "tone_or_style",
            "unnecessary_reply",
            "other",
        }:
            raise ValueError(f"unsupported feedback_reason: {feedback_reason}")
        if note is not None and len(note.strip()) > 500:
            raise ValueError("feedback note must be at most 500 characters")
        if verb in {"approve", "reject"}:
            allowed_outcomes = (
                {"suggestion_sent"}
                if verb == "approve"
                else {"no_send_keep_watching", "no_send_end_task"}
            )
            if (
                requested_outcome is not None
                and requested_outcome not in allowed_outcomes
            ):
                raise ValueError(
                    f"outcome {requested_outcome!r} is not valid for /{verb}"
                )
            if target_id.startswith("t_"):
                task = conn.execute(
                    "SELECT * FROM tasks WHERE short_id = ?",
                    (target_id,),
                ).fetchone()
                if task is not None:
                    pending = self._list_pending_approvals_locked(
                        conn, task_id=int(task["id"])
                    )
                    if len(pending) > 1:
                        return (
                            self._create_approval_command_conflict_notification_locked(
                                conn,
                                task_id=int(task["id"]),
                                task_short_id=target_id,
                                verb=verb,
                                pending=pending,
                                now=now,
                            )
                        )
            approval = self._resolve_pending_approval_locked(conn, target_id)
            if approval is None:
                raise ValueError(
                    f"pending approval not found or ambiguous: {target_id}"
                )
            resolved_status = (
                ApprovalStatus.APPROVED.value
                if verb == "approve"
                else ApprovalStatus.REJECTED.value
            )
            task = None
            if approval["task_id"] is not None:
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (approval["task_id"],)
                ).fetchone()
            if verb == "approve" and not _task_is_watching(task):
                raise ValueError("approval task is not watching")
            payload = json.loads(approval["payload_json"] or "{}")
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolved_status, now, approval["id"]),
            )
            if verb == "reject":
                keep_watching = (
                    requested_outcome == "no_send_keep_watching"
                    if requested_outcome is not None
                    else payload.get("keep_watching_on_reject") is True
                )
                outcome: ApprovalOutcome = (
                    "no_send_keep_watching" if keep_watching else "no_send_end_task"
                )
                if keep_watching and approval["task_id"] is not None:
                    cancelled_actions = (
                        self._cancel_pending_actions_for_approvals_locked(
                            conn,
                            approval_ids=[int(approval["id"])],
                            now=now,
                        )
                    )
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = ?,
                            updated_at = ?,
                            closed_at = NULL,
                            watch_until = COALESCE(?, watch_until)
                        WHERE id = ?
                        """,
                        (
                            TaskStatus.WATCHING.value,
                            now,
                            keep_watching_until,
                            int(approval["task_id"]),
                        ),
                    )
                    self._record_approval_feedback_locked(
                        conn,
                        approval=approval,
                        command_id=command_id,
                        outcome=outcome,
                        decision_reason=payload.get("decision_reason"),
                        suggested_reply=_payload_send_text(payload)
                        or approval["preview"],
                        final_reply=None,
                        feedback_reason=feedback_reason,
                        note=note,
                        actor=actor,
                        execution_mode=execution_mode,
                        now=now,
                    )
                    return {
                        "approval_id": approval["short_id"],
                        "task_id": approval["task_id"],
                        "action_id": None,
                        "kept_watching": True,
                        "cancelled_actions": cancelled_actions,
                        "outcome": outcome,
                    }
                if approval["task_id"] is not None:
                    self._close_task_after_reject_locked(
                        conn,
                        task_id=int(approval["task_id"]),
                        rejected_approval_id=int(approval["id"]),
                        now=now,
                    )
                self._record_approval_feedback_locked(
                    conn,
                    approval=approval,
                    command_id=command_id,
                    outcome=outcome,
                    decision_reason=payload.get("decision_reason"),
                    suggested_reply=_payload_send_text(payload) or approval["preview"],
                    final_reply=None,
                    feedback_reason=feedback_reason,
                    note=note,
                    actor=actor,
                    execution_mode=execution_mode,
                    now=now,
                )
                return {
                    "approval_id": approval["short_id"],
                    "task_id": approval["task_id"],
                    "action_id": None,
                    "outcome": outcome,
                }
            if payload.get("approvable") is False:
                raise ValueError("approval requires /send final reply")
            target_message_id = payload.get("reply_target_message_id") or payload.get(
                "target_message_id"
            )
            if not isinstance(target_message_id, str) or not target_message_id:
                raise ValueError("approval payload is missing reply_target_message_id")
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("approval payload is missing text; use /send")
            action_id = self._create_send_reply_action_locked(
                conn,
                task_id=int(approval["task_id"]),
                target_message_id=target_message_id,
                payload=payload | {"approved_by": "owner"},
                approval_id=int(approval["id"]),
                execution_mode=execution_mode,
                now=now,
            )
            if action_id is None:
                raise ValueError(
                    "active send action already exists for this task and reply target"
                )
            self._mark_task_watching_after_send_locked(
                conn, task_id=int(approval["task_id"]), now=now
            )
            outcome = "suggestion_sent"
            self._record_approval_feedback_locked(
                conn,
                approval=approval,
                command_id=command_id,
                outcome=outcome,
                decision_reason=payload.get("decision_reason"),
                suggested_reply=_payload_send_text(payload) or approval["preview"],
                final_reply=_payload_send_text(payload),
                feedback_reason=feedback_reason,
                note=note,
                actor=actor,
                execution_mode=execution_mode,
                now=now,
            )
            return {
                "approval_id": approval["short_id"],
                "task_id": approval["task_id"],
                "action_id": action_id,
                "outcome": outcome,
            }

        if verb == "send":
            if requested_outcome not in {None, "edited_sent"}:
                raise ValueError(
                    f"outcome {requested_outcome!r} is not valid for /send"
                )
            concrete_approval = None
            if target_id.startswith("a_"):
                concrete_approval = self._resolve_pending_approval_locked(
                    conn, target_id
                )
                if concrete_approval is None:
                    raise ValueError(f"pending approval not found: {target_id}")
                if concrete_approval["task_id"] is None:
                    raise ValueError("approval is not attached to a task")
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (concrete_approval["task_id"],),
                ).fetchone()
            elif target_id.startswith("t_"):
                task = conn.execute(
                    "SELECT * FROM tasks WHERE short_id = ?",
                    (target_id,),
                ).fetchone()
            else:
                raise ValueError("/send requires a task or approval id")
            if task is None:
                raise ValueError(f"task not found: {target_id}")
            if not _task_is_watching(task):
                raise ValueError(f"task is not watching: {target_id}")
            reply_text = final_reply or ""
            if not reply_text.strip():
                raise ValueError("/send requires final reply text")
            pending = (
                [concrete_approval]
                if concrete_approval is not None
                else self._list_pending_send_reply_approvals_locked(
                    conn, task_id=int(task["id"])
                )
            )
            if len(pending) > 1:
                return self._create_approval_command_conflict_notification_locked(
                    conn,
                    task_id=int(task["id"]),
                    task_short_id=target_id,
                    verb=verb,
                    pending=pending,
                    now=now,
                )
            target_message_id: Any
            approval_id: int
            approval_short_id: str
            if len(pending) == 1:
                previous_payload = json.loads(pending[0]["payload_json"] or "{}")
                target_message_id = previous_payload.get(
                    "reply_target_message_id"
                ) or previous_payload.get("target_message_id")
                approval_id = int(pending[0]["id"])
                approval_short_id = pending[0]["short_id"]
            else:
                target_message_id = task["root_message_id"]
                approval_short_id = self._unique_short_id_in_table(
                    conn, "approvals", "a", f"{target_id}:{reply_text}:{now}"
                )
                approval_id = 0
            if not isinstance(target_message_id, str) or not target_message_id:
                raise ValueError("task does not have a reply target")
            payload = {
                "reply_target_message_id": target_message_id,
                "text": reply_text,
                "identity": "user",
                "source": "owner_send",
            }
            original_payload = (
                json.loads(pending[0]["payload_json"] or "{}")
                if len(pending) == 1
                else {}
            )
            if approval_id:
                conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, resolved_at = ?, payload_json = ?, preview = ?
                    WHERE id = ?
                    """,
                    (
                        ApprovalStatus.APPROVED.value,
                        now,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        reply_text,
                        approval_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO approvals(
                      short_id, task_id, kind, status, payload_json, preview, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_short_id,
                        int(task["id"]),
                        ApprovalKind.SEND_REPLY.value,
                        ApprovalStatus.APPROVED.value,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        reply_text,
                        now,
                        now,
                    ),
                )
                approval_id = int(cursor.lastrowid)
            action_id = self._create_send_reply_action_locked(
                conn,
                task_id=int(task["id"]),
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                execution_mode=execution_mode,
                now=now,
            )
            if action_id is None:
                raise ValueError(
                    "active send action already exists for this task and reply target"
                )
            self._mark_task_watching_after_send_locked(
                conn, task_id=int(task["id"]), now=now
            )
            approval = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise RuntimeError("approved reply disappeared before feedback capture")
            outcome = "edited_sent"
            self._record_approval_feedback_locked(
                conn,
                approval=approval,
                command_id=command_id,
                outcome=outcome,
                decision_reason=original_payload.get("decision_reason"),
                suggested_reply=_payload_send_text(original_payload)
                or approval["preview"],
                final_reply=reply_text,
                feedback_reason=feedback_reason,
                note=note,
                actor=actor,
                execution_mode=execution_mode,
                now=now,
            )
            return {
                "approval_id": approval_short_id,
                "task_id": task["id"],
                "action_id": action_id,
                "outcome": outcome,
            }

        raise ValueError(f"unsupported command: {verb}")

    def _record_approval_feedback_locked(
        self,
        conn: sqlite3.Connection,
        *,
        approval: sqlite3.Row,
        command_id: str,
        outcome: ApprovalOutcome,
        decision_reason: Any,
        suggested_reply: Any,
        final_reply: Any,
        feedback_reason: FeedbackReason | None,
        note: str | None,
        actor: str,
        execution_mode: ExecutionMode,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO approval_feedback(
              approval_id, task_id, command_id, outcome, decision_reason,
              suggested_reply, final_reply, feedback_reason, note, actor,
              execution_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(approval["id"]),
                None if approval["task_id"] is None else int(approval["task_id"]),
                command_id,
                outcome,
                decision_reason if isinstance(decision_reason, str) else None,
                suggested_reply if isinstance(suggested_reply, str) else None,
                final_reply if isinstance(final_reply, str) else None,
                feedback_reason,
                note.strip() if isinstance(note, str) and note.strip() else None,
                actor,
                execution_mode,
                now,
            ),
        )

    def _keep_watching_reject_task_id_locked(
        self,
        conn: sqlite3.Connection,
        *,
        verb: str,
        target_id: str,
    ) -> int | None:
        if verb != "reject":
            return None
        if target_id.startswith("a_"):
            approval = conn.execute(
                """
                SELECT * FROM approvals
                WHERE short_id = ? AND status = 'pending'
                """,
                (target_id,),
            ).fetchone()
            if approval is None or approval["task_id"] is None:
                return None
            payload = _loads_json_object(approval["payload_json"])
            return (
                int(approval["task_id"])
                if payload.get("keep_watching_on_reject") is True
                else None
            )
        if target_id.startswith("t_"):
            task = conn.execute(
                "SELECT * FROM tasks WHERE short_id = ?", (target_id,)
            ).fetchone()
            if task is None:
                return None
            pending = self._list_pending_approvals_locked(conn, task_id=int(task["id"]))
            for approval in pending:
                payload = _loads_json_object(approval["payload_json"])
                if payload.get("keep_watching_on_reject") is True:
                    return int(task["id"])
        return None

    def _cancel_pending_actions_for_approvals_locked(
        self,
        conn: sqlite3.Connection,
        *,
        approval_ids: Iterable[int],
        now: str,
    ) -> int:
        ids = list(dict.fromkeys(int(approval_id) for approval_id in approval_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"""
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE approval_id IN ({placeholders})
              AND kind IN (?, ?)
              AND status = ?
            """,
            [
                ActionStatus.CANCELLED.value,
                now,
                *ids,
                ActionKind.SEND_REPLY.value,
                ActionKind.OWNER_NOTIFICATION.value,
                ActionStatus.PENDING.value,
            ],
        )
        return int(cursor.rowcount)

    def _close_task_after_reject_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        rejected_approval_id: int,
        now: str,
    ) -> None:
        pending_rows = conn.execute(
            """
            SELECT id
            FROM approvals
            WHERE task_id = ? AND status = 'pending'
            """,
            (task_id,),
        ).fetchall()
        pending_approval_ids = [int(row["id"]) for row in pending_rows]
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (TaskStatus.CLOSED.value, now, now, task_id),
        )
        conn.execute(
            """
            UPDATE approvals
            SET status = ?, resolved_at = ?
            WHERE task_id = ? AND status = 'pending'
            """,
            (ApprovalStatus.EXPIRED.value, now, task_id),
        )
        conn.execute(
            """
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND kind = ? AND status = ?
            """,
            (
                ActionStatus.CANCELLED.value,
                now,
                task_id,
                ActionKind.SEND_REPLY.value,
                ActionStatus.PENDING.value,
            ),
        )
        self._cancel_pending_actions_for_approvals_locked(
            conn,
            approval_ids=[rejected_approval_id, *pending_approval_ids],
            now=now,
        )

    def _mark_task_watching_after_send_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, closed_at = NULL
            WHERE id = ?
            """,
            (TaskStatus.WATCHING.value, now, task_id),
        )

    def _list_pending_approvals_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT * FROM approvals
            WHERE task_id = ? AND status = 'pending'
            ORDER BY created_at DESC, id DESC
            """,
            (task_id,),
        ).fetchall()

    def _list_pending_send_reply_approvals_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT * FROM approvals
            WHERE task_id = ? AND kind = 'send_reply' AND status = 'pending'
            ORDER BY created_at DESC, id DESC
            """,
            (task_id,),
        ).fetchall()

    def _create_approval_command_conflict_notification_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        task_short_id: str,
        verb: str,
        pending: list[sqlite3.Row],
        now: str,
    ) -> dict[str, Any]:
        pending_short_ids = [row["short_id"] for row in pending]
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        root_message = None
        if task is not None and task["root_message_id"]:
            root_message = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (task["root_message_id"],),
            ).fetchone()
        payload = {
            "type": "approval_command_conflict",
            "task_id": task_short_id,
            "reason": "multiple_pending_approvals",
            "pending_approval_ids": pending_short_ids,
            "pending_approvals": [
                _pending_approval_notification_payload(row, task_short_id=task_short_id)
                for row in pending
            ],
            "message": f"Multiple pending approvals exist for {task_short_id}; use a concrete a_ approval id.",
        }
        source = _owner_notification_source_payload(task, root_message)
        if any(value for value in source.values()):
            payload["source"] = source
        if task is not None and task["root_message_id"]:
            payload["incoming_message"] = _owner_notification_message_payload(
                root_message,
                fallback_message_id=task["root_message_id"],
            )
        action_id = self._create_owner_notification_action_locked(
            conn,
            task_id=task_id,
            payload=payload,
            now=now,
        )
        return {
            "_status": "failed",
            "error": f"/{verb} requires a concrete approval id for {task_short_id}",
            "notification_action_id": action_id,
            "pending_approval_ids": pending_short_ids,
        }

    def _resolve_pending_approval_locked(
        self,
        conn: sqlite3.Connection,
        target_id: str,
    ) -> sqlite3.Row | None:
        if target_id.startswith("a_"):
            return conn.execute(
                """
                SELECT * FROM approvals
                WHERE short_id = ? AND status = 'pending'
                """,
                (target_id,),
            ).fetchone()
        if target_id.startswith("t_"):
            rows = conn.execute(
                """
                SELECT a.*
                FROM approvals a
                JOIN tasks t ON t.id = a.task_id
                WHERE t.short_id = ? AND a.status = 'pending'
                ORDER BY a.created_at DESC, a.id DESC
                """,
                (target_id,),
            ).fetchall()
            if len(rows) == 1:
                return rows[0]
        return None

    def _create_send_reply_action_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        target_message_id: str,
        payload: dict[str, Any],
        approval_id: int | None,
        execution_mode: ExecutionMode,
        now: str,
    ) -> int | None:
        # A sent action for the same task/target/text is terminal idempotency.
        # Failed actions are revived to preserve the original idempotency key,
        # but only when no pending/sending action could race the same reply.
        if _has_sent_send_reply_action_for_payload(
            conn,
            task_id=task_id,
            target_message_id=target_message_id,
            payload=payload,
        ):
            return None
        failed = _find_failed_send_reply_action_for_payload(
            conn,
            task_id=task_id,
            target_message_id=target_message_id,
            payload=payload,
            execution_mode=execution_mode,
        )
        if failed is not None:
            if _has_active_send_reply_action(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                exclude_action_id=int(failed["id"]),
                execution_mode=execution_mode,
            ):
                return None
            _revive_failed_send_reply_action(
                conn,
                action_id=int(failed["id"]),
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                execution_mode=execution_mode,
                now=now,
            )
            return int(failed["id"])

        idempotency_key = _action_idempotency_key(
            task_id, target_message_id, payload, execution_mode=execution_mode
        )
        try:
            cursor = conn.execute(
                """
                INSERT INTO actions(
                  idempotency_key, task_id, approval_id, kind, status, target_message_id,
                  dry_run, execution_mode, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    task_id,
                    approval_id,
                    ActionKind.SEND_REPLY.value,
                    ActionStatus.PENDING.value,
                    target_message_id,
                    1,
                    execution_mode,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                """
                SELECT id, status
                FROM actions
                WHERE idempotency_key = ? AND kind = 'send_reply'
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None or row["status"] != ActionStatus.FAILED.value:
                return None
            if _has_active_send_reply_action(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                exclude_action_id=int(row["id"]),
                execution_mode=execution_mode,
            ):
                return None
            _revive_failed_send_reply_action(
                conn,
                action_id=int(row["id"]),
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                execution_mode=execution_mode,
                now=now,
            )
            return int(row["id"])
        return int(cursor.lastrowid)

    def _create_owner_notification_action_locked(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int | None,
        payload: dict[str, Any],
        approval_id: int | None = None,
        execution_mode: ExecutionMode = "production",
        now: str,
    ) -> int:
        dedupe_key = payload.get("dedupe_key")
        seed_value = (
            str(dedupe_key)
            if isinstance(dedupe_key, str) and dedupe_key
            else json.dumps(
                {
                    "task_id": task_id,
                    "payload": payload,
                    "execution_mode": execution_mode,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
        seed = f"{execution_mode}:{seed_value}"
        idempotency_key = f"owner-{sha256(seed.encode('utf-8')).hexdigest()[:16]}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO actions(
              idempotency_key, task_id, approval_id, kind, status, dry_run,
              execution_mode, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                task_id,
                approval_id,
                ActionKind.OWNER_NOTIFICATION.value,
                ActionStatus.PENDING.value,
                1,
                execution_mode,
                json.dumps(payload, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid)
        row = conn.execute(
            "SELECT id, status FROM actions WHERE idempotency_key = ? AND kind = 'owner_notification'",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "owner notification action was not inserted and no existing action was found"
            )
        if row["status"] == ActionStatus.FAILED.value:
            conn.execute(
                """
                UPDATE actions
                SET status = ?,
                    approval_id = COALESCE(?, approval_id),
                    dry_run = ?,
                    execution_mode = ?,
                    payload_json = ?,
                    result_json = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (
                    ActionStatus.PENDING.value,
                    approval_id,
                    1,
                    execution_mode,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                    row["id"],
                ),
            )
        return int(row["id"])

    def insert_action_for_test(
        self,
        *,
        idempotency_key: str,
        task_id: int,
        kind: str = "send_reply",
        status: str = "pending",
        result: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        now = self.clock()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO actions(
                  idempotency_key, task_id, kind, status, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    task_id,
                    kind,
                    status,
                    None
                    if result is None
                    else json.dumps(result, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )

    def insert_approval_for_test(
        self,
        *,
        short_id: str,
        task_id: int,
        kind: str = "send_reply",
        status: str = "pending",
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals(short_id, task_id, kind, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (short_id, task_id, kind, status, self.clock(), None),
            )

    def _insert_chat_policy_locked(
        self,
        conn: sqlite3.Connection,
        policy: dict[str, Any],
        *,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO chat_policies(
              chat_id, name, auto_reply, bot_joined, reply_identity,
              allow_user_fallback, resource_download, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy["chat_id"],
                policy["name"],
                int(policy["auto_reply"]),
                int(policy["bot_joined"]),
                policy["reply_identity"],
                int(policy["allow_user_fallback"]),
                int(policy["resource_download"]),
                now,
            ),
        )

    def _update_chat_policy_locked(
        self,
        conn: sqlite3.Connection,
        policy: dict[str, Any],
        *,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE chat_policies
            SET name = ?,
                auto_reply = ?,
                bot_joined = ?,
                reply_identity = ?,
                allow_user_fallback = ?,
                resource_download = ?,
                updated_at = ?
            WHERE chat_id = ?
            """,
            (
                policy["name"],
                int(policy["auto_reply"]),
                int(policy["bot_joined"]),
                policy["reply_identity"],
                int(policy["allow_user_fallback"]),
                int(policy["resource_download"]),
                now,
                policy["chat_id"],
            ),
        )

    def _record_policy_audit_locked(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        policy_key: str,
        old_policy: dict[str, Any] | None,
        new_policy: dict[str, Any] | None,
        actor: str,
        reason: str,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO policy_audits(
              scope, policy_key, actor, old_json, new_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                policy_key,
                actor,
                None if old_policy is None else _policy_json(old_policy),
                None if new_policy is None else _policy_json(new_policy),
                reason,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _get_task_by_id(self, conn: sqlite3.Connection, task_id: int) -> TaskRecord:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return _task_from_row(row)

    def _get_task_by_lookup(
        self, conn: sqlite3.Connection, task_id: int | str
    ) -> TaskRecord | None:
        if isinstance(task_id, int):
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        else:
            text = str(task_id)
            if text.isdigit():
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (int(text),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE short_id = ?", (text,)
                ).fetchone()
        return None if row is None else _task_from_row(row)

    def _create_task_for_message(
        self,
        conn: sqlite3.Connection,
        message: NormalizedMessage,
        *,
        watch_until: str,
        task_label: str | None,
        agent_working_dir: str | None,
        now: str,
    ) -> int:
        short_id = self._unique_short_id(conn, "t", message.message_id)
        label = task_label or _clean_label(message.text)
        cursor = conn.execute(
            """
            INSERT INTO tasks(
              short_id, status, chat_id, root_message_id, task_label, agent_session_id,
              agent_session_provider, agent_working_dir, created_at, updated_at, chat_type,
              thread_id, watch_until, last_user_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                TaskStatus.WATCHING.value,
                message.chat_id,
                message.message_id,
                label,
                None,
                None,
                agent_working_dir,
                now,
                now,
                message.chat_type,
                message.thread_id,
                watch_until,
                _truncate(message.text),
            ),
        )
        task_id = int(cursor.lastrowid)
        self._add_task_message(conn, task_id, message.message_id, "root", now)
        self._add_watch_keys(conn, task_id, _watch_keys_for_message(message), now)
        return task_id

    def _attach_message_to_task(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        message: NormalizedMessage,
        *,
        watch_until: str,
        now: str,
    ) -> None:
        self._add_task_message(conn, task_id, message.message_id, "follow_up", now)
        self._add_watch_keys(conn, task_id, _watch_keys_for_message(message), now)
        conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, watch_until = ?, last_user_message = ?
            WHERE id = ?
            """,
            (now, watch_until, _truncate(message.text), task_id),
        )

    def _close_task_for_owner_takeover(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        *,
        now: str,
    ) -> None:
        pending_rows = conn.execute(
            """
            SELECT id
            FROM approvals
            WHERE task_id = ? AND status = 'pending'
            """,
            (task_id,),
        ).fetchall()
        pending_approval_ids = [int(row["id"]) for row in pending_rows]
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (TaskStatus.HUMAN_TAKEN_OVER.value, now, now, task_id),
        )
        conn.execute(
            """
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND kind = ? AND status IN ('pending', 'sending')
            """,
            (ActionStatus.CANCELLED.value, now, task_id, ActionKind.SEND_REPLY.value),
        )
        conn.execute(
            """
            UPDATE approvals
            SET status = ?, resolved_at = ?
            WHERE task_id = ? AND status = 'pending'
            """,
            (ApprovalStatus.EXPIRED.value, now, task_id),
        )
        self._cancel_pending_actions_for_approvals_locked(
            conn,
            approval_ids=pending_approval_ids,
            now=now,
        )

    def _close_task_by_operator_locked(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        *,
        now: str,
    ) -> tuple[int, int]:
        pending_rows = conn.execute(
            """
            SELECT id
            FROM approvals
            WHERE task_id = ? AND status = 'pending'
            """,
            (task_id,),
        ).fetchall()
        pending_approval_ids = [int(row["id"]) for row in pending_rows]
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (TaskStatus.CLOSED_BY_OWNER.value, now, now, task_id),
        )
        approval_cursor = conn.execute(
            """
            UPDATE approvals
            SET status = ?, resolved_at = ?
            WHERE task_id = ? AND status = 'pending'
            """,
            (ApprovalStatus.EXPIRED.value, now, task_id),
        )
        send_cursor = conn.execute(
            """
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND kind = ? AND status IN ('pending', 'sending')
            """,
            (ActionStatus.CANCELLED.value, now, task_id, ActionKind.SEND_REPLY.value),
        )
        owner_cursor = conn.execute(
            """
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND kind = ? AND status = 'pending'
            """,
            (
                ActionStatus.CANCELLED.value,
                now,
                task_id,
                ActionKind.OWNER_NOTIFICATION.value,
            ),
        )
        approval_action_count = self._cancel_pending_actions_for_approvals_locked(
            conn,
            approval_ids=pending_approval_ids,
            now=now,
        )
        return int(approval_cursor.rowcount), (
            int(send_cursor.rowcount)
            + int(owner_cursor.rowcount)
            + approval_action_count
        )

    def _record_agent_message_for_task(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        message: NormalizedMessage,
        *,
        watch_until: str,
        now: str,
    ) -> None:
        self._add_task_message(conn, task_id, message.message_id, "agent_reply", now)
        self._add_watch_keys(
            conn, task_id, _agent_reply_watch_keys_for_message(message), now
        )
        conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, watch_until = ?, last_agent_reply = ?
            WHERE id = ?
            """,
            (now, watch_until, _truncate(message.text), task_id),
        )

    def _upsert_message_locked(
        self,
        conn: sqlite3.Connection,
        message: NormalizedMessage,
        *,
        now: str,
    ) -> bool:
        existing = conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            (message.message_id,),
        ).fetchone()
        normalized_json = json.dumps(
            {
                "mentions": message.mentions,
                "resources": [resource.raw for resource in message.resources],
                "thread_id": message.thread_id,
                "reply_to_message_id": message.reply_to_message_id,
                "direct_mention": message.direct_mention,
                "at_all": message.at_all,
                "sender_name": message.sender_name,
            },
            ensure_ascii=False,
            default=str,
        )
        raw_json = json.dumps(message.raw, ensure_ascii=False, default=str)
        if existing is None:
            conn.execute(
                """
                INSERT INTO messages(
                  message_id, chat_id, chat_type, sender_id, sender_type, sent_at,
                  normalized_json, raw_json, inserted_at, thread_id, reply_to_message_id,
                  sender_role, direct_mention, at_all, text, sender_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.chat_id,
                    message.chat_type,
                    message.sender_id,
                    message.sender_type,
                    message.sent_at,
                    normalized_json,
                    raw_json,
                    now,
                    message.thread_id,
                    message.reply_to_message_id,
                    message.sender_role,
                    int(message.direct_mention),
                    int(message.at_all),
                    message.text,
                    message.sender_name,
                ),
            )
            return True
        conn.execute(
            """
            UPDATE messages
            SET chat_id = ?, chat_type = ?, sender_id = ?, sender_type = ?, sent_at = ?,
                normalized_json = ?, raw_json = ?, thread_id = ?, reply_to_message_id = ?,
                sender_role = ?, direct_mention = ?, at_all = ?, text = ?, sender_name = ?
            WHERE message_id = ?
            """,
            (
                message.chat_id,
                message.chat_type,
                message.sender_id,
                message.sender_type,
                message.sent_at,
                normalized_json,
                raw_json,
                message.thread_id,
                message.reply_to_message_id,
                message.sender_role,
                int(message.direct_mention),
                int(message.at_all),
                message.text,
                message.sender_name,
                message.message_id,
            ),
        )
        return False

    def _record_routing_audit(
        self,
        conn: sqlite3.Connection,
        *,
        message_id: str,
        decision: RouteDecision,
    ) -> None:
        conn.execute(
            """
            INSERT INTO routing_audits(
              message_id, task_id, route, route_reason, candidates_count, shortcut_hit,
              router_called, matched_by, target_task_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                decision.target_task_id,
                decision.route,
                decision.reason,
                decision.candidates_count,
                int(decision.shortcut_hit),
                int(decision.router_called),
                decision.matched_by,
                decision.target_task_id,
                self.clock(),
            ),
        )

    def _unique_short_id(self, conn: sqlite3.Connection, prefix: str, seed: str) -> str:
        return self._unique_short_id_in_table(conn, "tasks", prefix, seed)

    def _unique_short_id_in_table(
        self,
        conn: sqlite3.Connection,
        table: str,
        prefix: str,
        seed: str,
    ) -> str:
        if table not in {"tasks", "approvals"}:
            raise ValueError("unsupported short id table")
        base = f"{prefix}_{sha256(seed.encode('utf-8')).hexdigest()[:8]}"
        existing = conn.execute(
            f"SELECT short_id FROM {table} WHERE short_id = ?",
            (base,),
        ).fetchone()
        if existing is None:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}_{suffix}"
            existing = conn.execute(
                f"SELECT short_id FROM {table} WHERE short_id = ?",
                (candidate,),
            ).fetchone()
            if existing is None:
                return candidate
        raise RuntimeError(f"could not allocate {prefix} short id")

    def _add_task_message(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        message_id: str,
        role: str,
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO task_messages(task_id, message_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, message_id, role, created_at),
        )

    def _add_watch_keys(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        keys: Iterable[str],
        created_at: str,
    ) -> None:
        conn.executemany(
            """
            INSERT OR IGNORE INTO task_watch_keys(task_id, key, created_at)
            VALUES (?, ?, ?)
            """,
            [(task_id, key, created_at) for key in sorted(set(keys))],
        )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        short_id=row["short_id"],
        status=row["status"],
        chat_id=row["chat_id"],
        chat_type=row["chat_type"],
        thread_id=row["thread_id"],
        root_message_id=row["root_message_id"],
        task_label=row["task_label"],
        watch_until=row["watch_until"],
        agent_session_id=row["agent_session_id"],
        agent_session_provider=row["agent_session_provider"],
        agent_working_dir=row["agent_working_dir"],
    )


def _task_command_summary(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.short_id,
        "task_short_id": task.short_id,
        "status": task.status,
        "chat_id": task.chat_id,
        "chat_type": task.chat_type,
        "watch_until": task.watch_until,
        "agent_session_provider": task.agent_session_provider,
        "agent_working_dir": task.agent_working_dir,
    }


def _action_from_row(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=int(row["id"]),
        idempotency_key=row["idempotency_key"],
        task_id=None if row["task_id"] is None else int(row["task_id"]),
        approval_id=None if row["approval_id"] is None else int(row["approval_id"]),
        kind=row["kind"],
        status=row["status"],
        target_message_id=row["target_message_id"],
        dry_run=bool(row["dry_run"]),
        execution_mode=row["execution_mode"],
        payload=_loads_json_object(row["payload_json"]),
        result=_loads_json_object(row["result_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _dispatch_attempt_from_row(row: sqlite3.Row) -> DispatchAttemptRecord:
    return DispatchAttemptRecord(
        id=int(row["id"]),
        action_id=int(row["action_id"]),
        run_id=row["run_id"],
        claim_token=row["claim_token"],
        status=row["status"],
        dry_run_result=_loads_json(row["dry_run_result_json"]),
        send_result=_loads_json(row["send_result_json"]),
        readback_result=_loads_json(row["readback_result_json"]),
        sent_message_id=row["sent_message_id"],
        error_stage=row["error_stage"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _action_record_dict(action: ActionRecord) -> dict[str, Any]:
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


def _dispatch_attempt_dict(attempt: DispatchAttemptRecord) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "action_id": attempt.action_id,
        "run_id": attempt.run_id,
        "claim_token": attempt.claim_token,
        "status": attempt.status,
        "dry_run_result": attempt.dry_run_result,
        "send_result": attempt.send_result,
        "readback_result": attempt.readback_result,
        "sent_message_id": attempt.sent_message_id,
        "error_stage": attempt.error_stage,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


def _global_product_policy_from_config(config: AppConfig) -> dict[str, Any]:
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


def _chat_policy_from_config(chat_id: str, config: ChatPolicyConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    return {"chat_id": chat_id, **data}


def _normalize_global_product_policy(policy: dict[str, Any]) -> dict[str, Any]:
    reply_policy = ReplyPolicyConfig.model_validate(
        policy.get("reply_policy")
    ).model_dump(mode="json")
    default_chat_policy = ChatPolicyConfig.model_validate(
        {
            "name": "",
            "auto_reply": False,
            **_loads_json_object(policy.get("default_chat_policy")),
        }
    ).model_dump(mode="json")
    return {
        "reply_policy": reply_policy,
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


def _normalize_chat_product_policy(policy: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(policy.get("chat_id", "")).strip()
    if not chat_id:
        raise ValueError("chat_id is required")
    chat_policy = ChatPolicyConfig.model_validate(
        {key: value for key, value in policy.items() if key != "chat_id"}
    ).model_dump(mode="json")
    return {"chat_id": chat_id, **chat_policy}


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


def _policy_json(policy: dict[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True, default=str)


def _row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _json_row_dict(row: sqlite3.Row | dict[str, Any], *columns: str) -> dict[str, Any]:
    data = _row_dict(row) or {}
    for column in columns:
        if column in data:
            data[column] = _loads_json(data[column])
    return data


def _task_context_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "role": row["role"],
        "chat_id": row["chat_id"],
        "chat_type": row["chat_type"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "sender_role": row["sender_role"],
        "sent_at": row["sent_at"],
        "thread_id": row["thread_id"],
        "reply_to_message_id": row["reply_to_message_id"],
        "text": row["text"],
    }


def _approval_read_model(
    row: sqlite3.Row | dict[str, Any],
    *,
    now: str,
    json_columns: tuple[str, ...] = (),
) -> dict[str, Any]:
    data = (
        _json_row_dict(row, *json_columns) if json_columns else (_row_dict(row) or {})
    )
    if data.get("status") == ApprovalStatus.PENDING.value:
        overdue_seconds = _approval_overdue_seconds(data.get("expires_at"), now=now)
        data["is_overdue"] = overdue_seconds > 0
        data["overdue_seconds"] = overdue_seconds
        data["recommended_action"] = "expire" if overdue_seconds > 0 else "review"
    return data


def _approval_overdue_seconds(expires_at: Any, *, now: str) -> int:
    expires_at_dt = _parse_datetime_or_none(expires_at)
    now_dt = _parse_datetime_or_none(now)
    if expires_at_dt is None or now_dt is None:
        return 0
    return max(0, int((now_dt - expires_at_dt).total_seconds()))


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
    return loaded if isinstance(loaded, dict) else {}


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


def _parse_datetime_or_none(value: Any) -> datetime | None:
    return parse_instant_or_none(value)


def _watch_keys_for_message(message: NormalizedMessage) -> set[str]:
    keys: set[str] = {f"msg:{message.message_id}"}
    if message.sender_id:
        keys.add(f"user:{message.sender_id}")
    if message.thread_id:
        keys.add(f"thread:{message.thread_id}")
    return keys


def _agent_reply_watch_keys_for_message(message: NormalizedMessage) -> set[str]:
    keys: set[str] = {f"msg:{message.message_id}"}
    if message.thread_id:
        keys.add(f"thread:{message.thread_id}")
    return keys


def _clean_label(text: str) -> str:
    return _truncate(" ".join(text.split()) or "未命名任务", limit=100)


def _truncate(text: str, limit: int = 100) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def _task_is_watching(row: sqlite3.Row | None) -> bool:
    return row is not None and row["status"] == TaskStatus.WATCHING.value


def _plus_hours(value: str, hours: int) -> str:
    return shift_instant(value, delta=timedelta(hours=hours))


def _minus_seconds(value: str, seconds: int) -> str:
    return shift_instant(value, delta=-timedelta(seconds=seconds))


def _action_idempotency_key(
    task_id: int,
    target_message_id: str,
    payload: dict[str, Any],
    *,
    execution_mode: ExecutionMode,
) -> str:
    seed = json.dumps(
        {
            "task_id": task_id,
            "target_message_id": target_message_id,
            "text": payload.get("text") or payload.get("composed_text") or "",
            "source": payload.get("source") or "",
            "execution_mode": execution_mode,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"reply-{sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _find_failed_send_reply_action_for_payload(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    target_message_id: str,
    payload: dict[str, Any],
    execution_mode: ExecutionMode,
) -> sqlite3.Row | None:
    text = _payload_send_text(payload)
    if not text:
        return None
    rows = conn.execute(
        """
        SELECT id, payload_json
        FROM actions
        WHERE task_id = ?
          AND target_message_id = ?
          AND kind = 'send_reply'
          AND status = 'failed'
          AND execution_mode = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (task_id, target_message_id, execution_mode),
    ).fetchall()
    for row in rows:
        if _payload_send_text(_loads_json_object(row["payload_json"])) == text:
            return row
    return None


def _has_sent_send_reply_action_for_payload(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    target_message_id: str,
    payload: dict[str, Any],
) -> bool:
    text = _payload_send_text(payload)
    if not text:
        return False
    rows = conn.execute(
        """
        SELECT payload_json
        FROM actions
        WHERE task_id = ?
          AND target_message_id = ?
          AND kind = 'send_reply'
          AND status = 'sent'
        """,
        (task_id, target_message_id),
    ).fetchall()
    return any(
        _payload_send_text(_loads_json_object(row["payload_json"])) == text
        for row in rows
    )


def _latest_dispatch_attempt_locked(
    conn: sqlite3.Connection, *, action_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM dispatch_attempts
        WHERE action_id = ?
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (action_id,),
    ).fetchone()


def _attempt_proves_readback(attempt: sqlite3.Row | None) -> bool:
    if attempt is None:
        return False
    readback = _loads_json_object(attempt["readback_result_json"])
    return (
        attempt["status"] == DispatchAttemptStatus.READBACK_OK.value
        and bool(attempt["sent_message_id"])
        and readback.get("ok") is True
    )


def _stale_sent_result(action: sqlite3.Row, attempt: sqlite3.Row) -> dict[str, Any]:
    result = _loads_json_object(action["result_json"])
    result["sent_message_id"] = attempt["sent_message_id"]
    readback = _loads_json_object(attempt["readback_result_json"])
    if readback:
        result["readback"] = readback
    warnings = _result_warnings(result)
    if "recovered_stale_sending_from_readback" not in warnings:
        warnings.append("recovered_stale_sending_from_readback")
    result["warnings"] = warnings
    return result


def _stale_needs_review_result(
    action: sqlite3.Row, attempt: sqlite3.Row | None
) -> dict[str, Any]:
    result = _loads_json_object(action["result_json"])
    result["error_stage"] = DispatchErrorStage.RECOVERY.value
    result["recovery_reason"] = "stale_sending_uncertain"
    if attempt is not None and attempt["sent_message_id"]:
        result["sent_message_id"] = attempt["sent_message_id"]
    warnings = _result_warnings(result)
    if "stale_sending_needs_review" not in warnings:
        warnings.append("stale_sending_needs_review")
    result["warnings"] = warnings
    return result


def _result_warnings(result: dict[str, Any]) -> list[str]:
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [str(warning) for warning in warnings]


def _has_active_send_reply_action(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    target_message_id: str,
    exclude_action_id: int,
    execution_mode: ExecutionMode,
) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM actions
        WHERE task_id = ?
          AND target_message_id = ?
          AND kind = 'send_reply'
          AND status IN ('pending', 'sending', 'failed_needs_review')
          AND execution_mode = ?
          AND id != ?
        LIMIT 1
        """,
        (task_id, target_message_id, execution_mode, exclude_action_id),
    ).fetchone()
    return row is not None


def _revive_failed_send_reply_action(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    task_id: int,
    target_message_id: str,
    payload: dict[str, Any],
    approval_id: int | None,
    execution_mode: ExecutionMode,
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE actions
        SET task_id = ?,
            approval_id = ?,
            status = ?,
            target_message_id = ?,
            dry_run = ?,
            execution_mode = ?,
            payload_json = ?,
            result_json = NULL,
            updated_at = ?
        WHERE id = ? AND status = 'failed'
        """,
        (
            task_id,
            approval_id,
            "pending",
            target_message_id,
            1,
            execution_mode,
            json.dumps(payload, ensure_ascii=False, default=str),
            now,
            action_id,
        ),
    )


def _payload_send_text(payload: dict[str, Any]) -> str:
    value = payload.get("text") or payload.get("composed_text") or ""
    return value if isinstance(value, str) else ""


def _approval_notification_payload(
    notify_payload: dict[str, Any],
    *,
    approval_short_id: str,
    approval_payload: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(notify_payload)
    task_short_id = payload.get("task_id")
    if isinstance(task_short_id, str) and task_short_id:
        commands = []
        if approval_payload.get("approvable") is not False:
            commands.append(f"/approve {approval_short_id}")
        commands.extend(
            [
                f"/send {task_short_id} <final reply>",
                f"/reject {approval_short_id}",
            ]
        )
        payload["commands"] = commands
    payload["approval_id"] = approval_short_id
    return payload


def _pending_approval_notification_payload(
    row: sqlite3.Row,
    *,
    task_short_id: str,
) -> dict[str, Any]:
    payload = _loads_json_object(row["payload_json"])
    approval_short_id = row["short_id"]
    kind = row["kind"]
    approvable = payload.get("approvable") is not False
    commands: list[str] = []
    if approvable:
        commands.append(f"/approve {approval_short_id}")
    if kind == "send_reply":
        commands.append(f"/send {task_short_id} <final reply>")
    commands.append(f"/reject {approval_short_id}")
    return {
        "approval_id": approval_short_id,
        "kind": kind,
        "reason": payload.get("reason") or "",
        "preview": row["preview"] or "",
        "approvable": approvable,
        "commands": commands,
    }


def _owner_notification_source_payload(
    task: sqlite3.Row | None,
    message: sqlite3.Row | None,
) -> dict[str, Any]:
    return {
        "task_label": None if task is None else task["task_label"],
        "chat_id": _row_value(message, "chat_id")
        or (None if task is None else task["chat_id"]),
        "chat_type": _row_value(message, "chat_type")
        or (None if task is None else task["chat_type"]),
        "sender_name": _row_value(message, "sender_name"),
        "sender_id": _row_value(message, "sender_id"),
        "sent_at": _row_value(message, "sent_at"),
    }


def _owner_notification_message_payload(
    message: sqlite3.Row | None,
    *,
    fallback_message_id: str,
) -> dict[str, Any]:
    return {
        "message_id": _row_value(message, "message_id") or fallback_message_id,
        "text": _row_value(message, "text") or "",
    }


def _row_value(row: sqlite3.Row | None, key: str) -> Any | None:
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _closed_recall_text_patterns(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))

    patterns: list[str] = []
    for token in tokens:
        if len(token) < 2:
            continue
        patterns.append(f"%{token[:24]}%")
        if len(patterns) >= 5:
            break
    return patterns


def _action_result_refs_message(result_json: str | None, message_id: str) -> bool:
    if not result_json:
        return False
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        return False
    if _message_ref_matches(result, {"sent_message_id", "sentMessageId"}, message_id):
        return True
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return _message_ref_matches(
            result["data"], {"message_id", "messageId"}, message_id
        )
    return False


def _message_ref_matches(value: Any, keys: set[str], message_id: str) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child == message_id:
                return True
            if _message_ref_matches(child, keys, message_id):
                return True
    elif isinstance(value, list):
        for child in value:
            if _message_ref_matches(child, keys, message_id):
                return True
    return False
