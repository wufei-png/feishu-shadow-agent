from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from ..types import (
    ActionRecord,
    HealthCheckResult,
    NormalizedMessage,
    ResourceRef,
    RouteDecision,
    TaskRecord,
    utc_now_iso,
)


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            migration_dir = resources.files("feishu_shadow_agent.store").joinpath("migrations")
            for migration in sorted(
                path for path in migration_dir.iterdir() if path.name.endswith(".sql")
            ):
                version = migration.name.removesuffix(".sql")
                if self._migration_applied(conn, version):
                    continue
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now_iso()),
                )

    def _migration_applied(self, conn: sqlite3.Connection, version: str) -> bool:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if table is None:
            return False
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        return row is not None

    def health_probe(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def record_run_start(
        self,
        *,
        run_id: str,
        dry_run: bool,
        git_commit: str | None = None,
        git_dirty: bool | None = None,
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs(
                  run_id, started_at, finished_at, status, dry_run, git_commit, git_dirty
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now_iso(),
                    "running",
                    int(dry_run),
                    git_commit,
                    None if git_dirty is None else int(git_dirty),
                ),
            )

    def record_run_finish(
        self,
        *,
        run_id: str,
        status: str,
        health_summary: dict[str, Any] | None = None,
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, health_summary_json = ?
                WHERE run_id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    json.dumps(health_summary or {}, ensure_ascii=False, default=str),
                    run_id,
                ),
            )

    def record_health_results(
        self,
        *,
        run_id: str | None,
        results: Iterable[HealthCheckResult],
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            if run_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs(run_id, started_at, status, dry_run)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, utc_now_iso(), "running", 1),
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
                        utc_now_iso(),
                    )
                    for result in results
                ],
            )

    def set_checkpoint(self, key: str, value: dict[str, Any]) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False, default=str), utc_now_iso()),
            )

    def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM checkpoints WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def upsert_message(self, message: NormalizedMessage) -> bool:
        self.migrate()
        existing = self.get_message(message.message_id)
        now = utc_now_iso()
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
        with self.connect() as conn:
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

    def get_message(self, message_id: str) -> sqlite3.Row | None:
        self.migrate()
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()

    def get_messages_by_ids(self, message_ids: Iterable[str]) -> list[sqlite3.Row]:
        self.migrate()
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE message_id IN ({placeholders}) ORDER BY sent_at, message_id",
                ids,
            ).fetchall()
        by_id = {row["message_id"]: row for row in rows}
        return [by_id[message_id] for message_id in ids if message_id in by_id]

    def message_has_routing_audit(self, message_id: str) -> bool:
        self.migrate()
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
        self.migrate()
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
                task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (target_task_id,)).fetchone()
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
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM message_processing
                WHERE message_id = ?
                  AND stage = ?
                  AND status IN ('processed', 'processing_failed_terminal')
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
        self.migrate()
        now = utc_now_iso()
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
        self.migrate()
        now = utc_now_iso()
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

    def count_prunable_message_raw_json(self, *, cutoff: str, replacement_json: str) -> int:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM messages
                WHERE datetime(inserted_at) <= datetime(?)
                  AND raw_json != ?
                """,
                (cutoff, replacement_json),
            ).fetchone()
        return int(row["count"])

    def prune_message_raw_json(self, *, cutoff: str, replacement_json: str) -> int:
        self.migrate()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages
                SET raw_json = ?
                WHERE datetime(inserted_at) <= datetime(?)
                  AND raw_json != ?
                """,
                (replacement_json, cutoff, replacement_json),
            )
        return int(cursor.rowcount)

    def list_prunable_resources(self, *, cutoff: str) -> list[sqlite3.Row]:
        self.migrate()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT r.*
                FROM resources r
                WHERE r.download_status = 'downloaded'
                  AND r.path IS NOT NULL
                  AND datetime(r.updated_at) <= datetime(?)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tasks t
                    LEFT JOIN task_messages tm ON tm.task_id = t.id
                    WHERE (t.root_message_id = r.message_id OR tm.message_id = r.message_id)
                      AND t.status IN ('watching', 'waiting_approval')
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
        self.migrate()
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
                [utc_now_iso(), *ids],
            )
        return int(cursor.rowcount)

    def create_task_for_message(
        self,
        message: NormalizedMessage,
        *,
        watch_until: str,
        task_label: str | None = None,
    ) -> TaskRecord:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            task_id = self._create_task_for_message(
                conn,
                message,
                watch_until=watch_until,
                task_label=task_label,
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
    ) -> tuple[TaskRecord, RouteDecision]:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            task_id = self._create_task_for_message(
                conn,
                message,
                watch_until=watch_until,
                task_label=None,
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
            self._record_routing_audit(conn, message_id=message.message_id, decision=decision)
        return task, decision

    def attach_message_to_task(self, task_id: int, message: NormalizedMessage, *, watch_until: str) -> None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            self._attach_message_to_task(conn, task_id, message, watch_until=watch_until, now=now)

    def attach_message_to_task_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
        *,
        watch_until: str,
        candidates_count: int,
        matched_by: str,
    ) -> RouteDecision:
        self.migrate()
        now = utc_now_iso()
        decision = RouteDecision(
            "attach_task",
            target_task_id=task.id,
            target_task_short_id=task.short_id,
            reason="deterministic_shortcut",
            candidates_count=candidates_count,
            shortcut_hit=True,
            matched_by=matched_by,
        )
        with self.connect() as conn:
            self._attach_message_to_task(conn, task.id, message, watch_until=watch_until, now=now)
            self._record_routing_audit(conn, message_id=message.message_id, decision=decision)
        return decision

    def close_task_for_owner_takeover(self, task_id: int) -> None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            self._close_task_for_owner_takeover(conn, task_id, now=now)

    def close_task_for_owner_takeover_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
    ) -> RouteDecision:
        self.migrate()
        now = utc_now_iso()
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
            self._record_routing_audit(conn, message_id=message.message_id, decision=decision)
        return decision

    def get_task_by_id(self, task_id: int) -> TaskRecord:
        self.migrate()
        with self.connect() as conn:
            return self._get_task_by_id(conn, task_id)

    def get_task_by_short_id(self, short_id: str) -> TaskRecord | None:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE short_id = ?",
                (short_id,),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def get_active_tasks_for_chat(self, chat_id: str, *, now: str) -> list[TaskRecord]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE chat_id = ?
                  AND status IN ('watching', 'waiting_approval')
                  AND (watch_until IS NULL OR watch_until > ?)
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
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*
                FROM tasks t
                JOIN task_watch_keys wk ON wk.task_id = t.id
                WHERE t.chat_id = ?
                  AND wk.key = ?
                  AND t.status IN ('watching', 'waiting_approval')
                  AND (t.watch_until IS NULL OR t.watch_until > ?)
                ORDER BY t.updated_at DESC, t.id DESC
                """,
                (chat_id, key, now),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def get_recent_closed_tasks(self, chat_id: str, *, limit: int = 20) -> list[TaskRecord]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE chat_id = ?
                  AND status NOT IN ('watching', 'waiting_approval')
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
                  AND t.status NOT IN ('watching', 'waiting_approval')
                  AND datetime(t.updated_at) >= datetime(?)
                  AND ({where_related})
                ORDER BY t.updated_at DESC, t.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def find_task_ids_for_message(self, message_id: str) -> list[int]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM task_messages WHERE message_id = ? ORDER BY task_id",
                (message_id,),
            ).fetchall()
        return [int(row["task_id"]) for row in rows]

    def find_task_for_sent_action_message(self, message_id: str) -> TaskRecord | None:
        self.migrate()
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
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            self._record_agent_message_for_task(conn, task_id, message, watch_until=watch_until, now=now)

    def record_agent_message_for_task_and_audit(
        self,
        task: TaskRecord,
        message: NormalizedMessage,
        *,
        watch_until: str,
    ) -> RouteDecision:
        self.migrate()
        now = utc_now_iso()
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
            self._record_routing_audit(conn, message_id=message.message_id, decision=decision)
        return decision

    def list_active_watch_targets(self, *, now: str) -> list[dict[str, str | None]]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT
                  t.chat_id AS chat_id,
                  t.chat_type AS chat_type,
                  substr(wk.key, ?) AS thread_id
                FROM tasks t
                JOIN task_watch_keys wk ON wk.task_id = t.id
                WHERE t.status IN ('watching', 'waiting_approval')
                  AND (t.watch_until IS NULL OR t.watch_until > ?)
                  AND t.chat_id IS NOT NULL
                  AND wk.key LIKE 'thread:%'
                  AND length(wk.key) > ?
                UNION
                SELECT DISTINCT
                  t.chat_id AS chat_id,
                  t.chat_type AS chat_type,
                  NULL AS thread_id
                FROM tasks t
                WHERE t.status IN ('watching', 'waiting_approval')
                  AND (t.watch_until IS NULL OR t.watch_until > ?)
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
        self.migrate()
        with self.connect() as conn:
            self._record_routing_audit(conn, message_id=message_id, decision=decision)

    def add_task_watch_keys(self, task_id: int, keys: Iterable[str]) -> None:
        self.migrate()
        unique_keys = sorted(set(keys))
        if not unique_keys:
            return
        with self.connect() as conn:
            self._add_watch_keys(conn, task_id, unique_keys, utc_now_iso())

    def has_resource_eligible_routing_audit(self, message_id: str) -> bool:
        self.migrate()
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
        self.migrate()
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
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM actions WHERE status IN ('pending', 'sending')"
            ).fetchone()
        return int(row["count"])

    def list_dispatchable_actions(self, *, limit: int = 50, kind: str | None = None) -> list[ActionRecord]:
        self.migrate()
        with self.connect() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM actions
                    WHERE status = 'pending'
                      AND kind IN ('send_reply', 'owner_notification')
                    ORDER BY created_at, id
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
                    ORDER BY created_at, id
                    LIMIT ?
                    """,
                    (kind, limit),
                ).fetchall()
        return [_action_from_row(row) for row in rows]

    def get_action(self, action_id: int) -> ActionRecord | None:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else _action_from_row(row)

    def claim_action_for_dispatch(self, action_id: int) -> ActionRecord | None:
        self.migrate()
        now = utc_now_iso()
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
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else _action_from_row(row)

    def record_action_preview(self, action_id: int, result: dict[str, Any]) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE actions
                SET result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (json.dumps(result, ensure_ascii=False, default=str), utc_now_iso(), action_id),
            )

    def finish_action(
        self,
        action_id: int,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        self.migrate()
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
                    utc_now_iso(),
                    action_id,
                ),
            )

    def status_snapshot(self, *, stale_after_seconds: int = 900) -> dict[str, Any]:
        self.migrate()
        with self.connect() as conn:
            last_run = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            pending_approvals = conn.execute(
                """
                SELECT a.short_id, a.task_id, t.short_id AS task_short_id, a.kind, a.preview, a.created_at
                FROM approvals a
                LEFT JOIN tasks t ON t.id = a.task_id
                WHERE a.status = 'pending'
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 20
                """
            ).fetchall()
            failed_commands = conn.execute(
                """
                SELECT message_id, command, status, result_json, updated_at
                FROM approval_commands
                WHERE status != 'applied' AND status != 'duplicate'
                ORDER BY updated_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            active_tasks = conn.execute(
                """
                SELECT id, short_id, status, chat_id, task_label, updated_at, watch_until
                FROM tasks
                WHERE status IN ('watching', 'waiting_approval')
                ORDER BY updated_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            pending_actions = conn.execute(
                """
                SELECT id, kind, status, task_id, target_message_id, updated_at, result_json
                FROM actions
                WHERE status IN ('pending', 'sending')
                ORDER BY updated_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            failed_actions = conn.execute(
                """
                SELECT id, kind, status, task_id, target_message_id, updated_at, result_json
                FROM actions
                WHERE status = 'failed'
                ORDER BY updated_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            recent_health = conn.execute(
                """
                SELECT check_name, severity, status, message, checked_at
                FROM health_checks
                WHERE status != 'ok'
                ORDER BY checked_at DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            stale_sending = conn.execute(
                """
                SELECT id, kind, status, task_id, target_message_id, updated_at
                FROM actions
                WHERE status = 'sending'
                  AND datetime(updated_at) <= datetime('now', ?)
                ORDER BY updated_at, id
                LIMIT 20
                """,
                (f"-{stale_after_seconds} seconds",),
            ).fetchall()
        return {
            "last_run": _row_dict(last_run),
            "pending_approvals": [_row_dict(row) for row in pending_approvals],
            "failed_approval_commands": [_json_row_dict(row, "result_json") for row in failed_commands],
            "active_tasks": [_row_dict(row) for row in active_tasks],
            "pending_actions": [_json_row_dict(row, "result_json") for row in pending_actions],
            "stale_sending_actions": [_row_dict(row) for row in stale_sending],
            "recent_failed_actions": [_json_row_dict(row, "result_json") for row in failed_actions],
            "recent_health_warnings": [_row_dict(row) for row in recent_health],
        }

    def replay_summary(self, message_id: str) -> dict[str, Any] | None:
        self.migrate()
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
        return {
            "message": _row_dict(message),
            "routing_audits": [_row_dict(row) for row in audits],
            "task_ids": task_ids,
            "actions": [_json_row_dict(row, "payload_json", "result_json") for row in actions],
        }

    def list_task_message_ids(self, task_id: int) -> list[str]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT message_id FROM task_messages WHERE task_id = ? ORDER BY created_at, message_id",
                (task_id,),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def list_resources_for_messages(self, message_ids: Iterable[str]) -> list[sqlite3.Row]:
        self.migrate()
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

    def get_initialized_hermes_session_id(self, task_id: int) -> str | None:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT hermes_session_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        session_id = row["hermes_session_id"]
        if not session_id or str(session_id).startswith("feishu-task-"):
            return None
        return str(session_id)

    def set_task_hermes_session_id(self, task_id: int, session_id: str) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET hermes_session_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_id, utc_now_iso(), task_id),
            )

    def update_task_after_hermes(
        self,
        *,
        task_id: int,
        task_label: str | None = None,
        status: str | None = None,
        watch_until: str | None = None,
    ) -> None:
        self.migrate()
        assignments = ["updated_at = ?"]
        params: list[Any] = [utc_now_iso()]
        if task_label is not None:
            assignments.append("task_label = ?")
            params.append(_truncate(task_label, limit=100))
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
            if status in {"watching", "waiting_approval"}:
                assignments.append("closed_at = NULL")
            else:
                assignments.append("closed_at = ?")
                params.append(utc_now_iso())
        if watch_until is not None:
            assignments.append("watch_until = ?")
            params.append(watch_until)
        params.append(task_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def record_hermes_audit(
        self,
        *,
        request_type: str,
        task_id: int | None,
        hermes_session_id: str | None,
        input_message_ids: Iterable[str],
        input_resource_ids: Iterable[str],
        response: dict[str, Any] | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
        prompt: dict[str, Any] | None = None,
        tool_permissions_profile: str | None = None,
    ) -> None:
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO hermes_audits(
                  request_type, task_id, hermes_session_id, input_message_ids_json,
                  input_resource_ids_json, response_json, error, latency_ms, prompt_json,
                  tool_permissions_profile, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_type,
                    task_id,
                    hermes_session_id,
                    json.dumps(list(input_message_ids), ensure_ascii=False, default=str),
                    json.dumps(list(input_resource_ids), ensure_ascii=False, default=str),
                    None if response is None else json.dumps(response, ensure_ascii=False, default=str),
                    error,
                    latency_ms,
                    None if prompt is None else json.dumps(prompt, ensure_ascii=False, default=str),
                    tool_permissions_profile,
                    utc_now_iso(),
                ),
            )

    def create_send_reply_action(
        self,
        *,
        task_id: int,
        target_message_id: str,
        payload: dict[str, Any],
        approval_id: int | None = None,
    ) -> int | None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            return self._create_send_reply_action_locked(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                now=now,
            )

    def create_owner_notification_action(
        self,
        *,
        task_id: int | None,
        payload: dict[str, Any],
    ) -> int:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            return self._create_owner_notification_action_locked(
                conn,
                task_id=task_id,
                payload=payload,
                now=now,
            )

    def create_send_reply_approval(
        self,
        *,
        task_id: int,
        preview: str,
        payload: dict[str, Any],
        notify_payload: dict[str, Any] | None = None,
    ) -> int:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            short_id = self._unique_short_id_in_table(conn, "approvals", "a", f"{task_id}:{preview}:{now}")
            cursor = conn.execute(
                """
                INSERT INTO approvals(short_id, task_id, kind, status, payload_json, preview, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    short_id,
                    task_id,
                    "send_reply",
                    "pending",
                    json.dumps(payload, ensure_ascii=False, default=str),
                    preview,
                    now,
                ),
            )
            approval_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                ("waiting_approval", now, task_id),
            )
            if notify_payload is not None:
                self._create_owner_notification_action_locked(
                    conn,
                    task_id=task_id,
                    payload=_approval_notification_payload(
                        notify_payload,
                        approval_short_id=short_id,
                        approval_payload=payload,
                    ),
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
    ) -> dict[str, Any]:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT status, result_json FROM approval_commands WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                return {"status": "duplicate", "result": json.loads(existing["result_json"] or "{}")}

            conn.execute("SAVEPOINT approval_command")
            try:
                result = self._apply_approval_command_locked(
                    conn,
                    verb=verb,
                    target_id=target_id,
                    final_reply=final_reply,
                    now=now,
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

    def _apply_approval_command_locked(
        self,
        conn: sqlite3.Connection,
        *,
        verb: str,
        target_id: str,
        final_reply: str | None,
        now: str,
    ) -> dict[str, Any]:
        if verb in {"approve", "reject"}:
            if target_id.startswith("t_"):
                task = conn.execute(
                    "SELECT * FROM tasks WHERE short_id = ?",
                    (target_id,),
                ).fetchone()
                if task is not None:
                    pending = self._list_pending_approvals_locked(conn, task_id=int(task["id"]))
                    if len(pending) > 1:
                        return self._create_approval_command_conflict_notification_locked(
                            conn,
                            task_id=int(task["id"]),
                            task_short_id=target_id,
                            verb=verb,
                            pending=pending,
                            now=now,
                        )
            approval = self._resolve_pending_approval_locked(conn, target_id)
            if approval is None:
                raise ValueError(f"pending approval not found or ambiguous: {target_id}")
            resolved_status = "approved" if verb == "approve" else "rejected"
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolved_status, now, approval["id"]),
            )
            if verb == "reject":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    ("closed", now, now, approval["task_id"]),
                )
                return {"approval_id": approval["short_id"], "task_id": approval["task_id"], "action_id": None}
            payload = json.loads(approval["payload_json"] or "{}")
            if payload.get("approvable") is False:
                raise ValueError("approval requires /send final reply")
            target_message_id = payload.get("reply_target_message_id") or payload.get("target_message_id")
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
                now=now,
            )
            if action_id is None:
                raise ValueError("active send action already exists for this task and reply target")
            self._mark_task_watching_after_send_locked(conn, task_id=int(approval["task_id"]), now=now)
            return {"approval_id": approval["short_id"], "task_id": approval["task_id"], "action_id": action_id}

        if verb == "send":
            if not target_id.startswith("t_"):
                raise ValueError("/send requires a task id")
            task = conn.execute(
                "SELECT * FROM tasks WHERE short_id = ?",
                (target_id,),
            ).fetchone()
            if task is None:
                raise ValueError(f"task not found: {target_id}")
            reply_text = final_reply or ""
            if not reply_text.strip():
                raise ValueError("/send requires final reply text")
            pending = self._list_pending_send_reply_approvals_locked(conn, task_id=int(task["id"]))
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
                target_message_id = previous_payload.get("reply_target_message_id") or previous_payload.get("target_message_id")
                approval_id = int(pending[0]["id"])
                approval_short_id = pending[0]["short_id"]
            else:
                target_message_id = task["root_message_id"]
                approval_short_id = self._unique_short_id_in_table(conn, "approvals", "a", f"{target_id}:{reply_text}:{now}")
                approval_id = 0
            if not isinstance(target_message_id, str) or not target_message_id:
                raise ValueError("task does not have a reply target")
            payload = {
                "reply_target_message_id": target_message_id,
                "text": reply_text,
                "identity": "user",
                "source": "owner_send",
            }
            if approval_id:
                conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, resolved_at = ?, payload_json = ?, preview = ?
                    WHERE id = ?
                    """,
                    (
                        "approved",
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
                        "send_reply",
                        "approved",
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
                now=now,
            )
            if action_id is None:
                raise ValueError("active send action already exists for this task and reply target")
            self._mark_task_watching_after_send_locked(conn, task_id=int(task["id"]), now=now)
            return {"approval_id": approval_short_id, "task_id": task["id"], "action_id": action_id}

        raise ValueError(f"unsupported command: {verb}")

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
            ("watching", now, task_id),
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
        action_id = self._create_owner_notification_action_locked(
            conn,
            task_id=task_id,
            payload={
                "type": "approval_command_conflict",
                "task_id": task_short_id,
                "reason": "multiple_pending_approvals",
                "pending_approval_ids": pending_short_ids,
                "message": f"Multiple pending approvals exist for {task_short_id}; use a concrete a_ approval id.",
            },
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
        )
        if failed is not None:
            if _has_active_send_reply_action(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                exclude_action_id=int(failed["id"]),
            ):
                return None
            _revive_failed_send_reply_action(
                conn,
                action_id=int(failed["id"]),
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
                now=now,
            )
            return int(failed["id"])

        idempotency_key = _action_idempotency_key(task_id, target_message_id, payload)
        try:
            cursor = conn.execute(
                """
                INSERT INTO actions(
                  idempotency_key, task_id, approval_id, kind, status, target_message_id,
                  dry_run, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    task_id,
                    approval_id,
                    "send_reply",
                    "pending",
                    target_message_id,
                    1,
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
            if row is None or row["status"] != "failed":
                return None
            if _has_active_send_reply_action(
                conn,
                task_id=task_id,
                target_message_id=target_message_id,
                exclude_action_id=int(row["id"]),
            ):
                return None
            _revive_failed_send_reply_action(
                conn,
                action_id=int(row["id"]),
                task_id=task_id,
                target_message_id=target_message_id,
                payload=payload,
                approval_id=approval_id,
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
        now: str,
    ) -> int:
        dedupe_key = payload.get("dedupe_key")
        seed = (
            str(dedupe_key)
            if isinstance(dedupe_key, str) and dedupe_key
            else json.dumps({"task_id": task_id, "payload": payload}, ensure_ascii=False, sort_keys=True, default=str)
        )
        idempotency_key = f"owner-{sha256(seed.encode('utf-8')).hexdigest()[:16]}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO actions(
              idempotency_key, task_id, kind, status, dry_run, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                task_id,
                "owner_notification",
                "pending",
                1,
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
            raise RuntimeError("owner notification action was not inserted and no existing action was found")
        if row["status"] == "failed":
            conn.execute(
                """
                UPDATE actions
                SET status = ?,
                    dry_run = ?,
                    payload_json = ?,
                    result_json = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (
                    "pending",
                    1,
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
        self.migrate()
        now = utc_now_iso()
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
                    None if result is None else json.dumps(result, ensure_ascii=False, default=str),
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
        self.migrate()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals(short_id, task_id, kind, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (short_id, task_id, kind, status, utc_now_iso()),
            )

    def _get_task_by_id(self, conn: sqlite3.Connection, task_id: int) -> TaskRecord:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return _task_from_row(row)

    def _create_task_for_message(
        self,
        conn: sqlite3.Connection,
        message: NormalizedMessage,
        *,
        watch_until: str,
        task_label: str | None,
        now: str,
    ) -> int:
        short_id = self._unique_short_id(conn, "t", message.message_id)
        label = task_label or _clean_label(message.text)
        cursor = conn.execute(
            """
            INSERT INTO tasks(
              short_id, status, chat_id, root_message_id, task_label, hermes_session_id,
              created_at, updated_at, chat_type, thread_id, watch_until, last_user_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                "watching",
                message.chat_id,
                message.message_id,
                label,
                None,
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
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            ("human_taken_over", now, now, task_id),
        )
        conn.execute(
            """
            UPDATE actions
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND kind = ? AND status IN ('pending', 'sending')
            """,
            ("cancelled", now, task_id, "send_reply"),
        )
        conn.execute(
            """
            UPDATE approvals
            SET status = ?, resolved_at = ?
            WHERE task_id = ? AND kind = ? AND status = 'pending'
            """,
            ("expired", now, task_id, "send_reply"),
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
        self._add_watch_keys(conn, task_id, _agent_reply_watch_keys_for_message(message), now)
        conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, watch_until = ?, last_agent_reply = ?
            WHERE id = ?
            """,
            (now, watch_until, _truncate(message.text), task_id),
        )

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
                utc_now_iso(),
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
        hermes_session_id=row["hermes_session_id"],
    )


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
        payload=_loads_json_object(row["payload_json"]),
        result=_loads_json_object(row["result_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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
    return f"{cleaned[:limit - 3]}..."


def _action_idempotency_key(task_id: int, target_message_id: str, payload: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "task_id": task_id,
            "target_message_id": target_message_id,
            "text": payload.get("text") or payload.get("composed_text") or "",
            "source": payload.get("source") or "",
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
        ORDER BY updated_at DESC, id DESC
        """,
        (task_id, target_message_id),
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
    return any(_payload_send_text(_loads_json_object(row["payload_json"])) == text for row in rows)


def _has_active_send_reply_action(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    target_message_id: str,
    exclude_action_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM actions
        WHERE task_id = ?
          AND target_message_id = ?
          AND kind = 'send_reply'
          AND status IN ('pending', 'sending')
          AND id != ?
        LIMIT 1
        """,
        (task_id, target_message_id, exclude_action_id),
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
        return _message_ref_matches(result["data"], {"message_id", "messageId"}, message_id)
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
