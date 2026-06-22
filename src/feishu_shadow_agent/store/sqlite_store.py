from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from ..types import HealthCheckResult, NormalizedMessage, ResourceRef, RouteDecision, TaskRecord, utc_now_iso


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
                      sender_role, direct_mention, at_all, text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                return True
            conn.execute(
                """
                UPDATE messages
                SET chat_id = ?, chat_type = ?, sender_id = ?, sender_type = ?, sent_at = ?,
                    normalized_json = ?, raw_json = ?, thread_id = ?, reply_to_message_id = ?,
                    sender_role = ?, direct_mention = ?, at_all = ?, text = ?
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

    def create_task_for_message(
        self,
        message: NormalizedMessage,
        *,
        watch_until: str,
        task_label: str | None = None,
    ) -> TaskRecord:
        self.migrate()
        now = utc_now_iso()
        short_id = self._unique_short_id("t", message.message_id)
        label = task_label or _clean_label(message.text)
        with self.connect() as conn:
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
                    f"feishu-task-{short_id}",
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
        return self.get_task_by_id(task_id)

    def attach_message_to_task(self, task_id: int, message: NormalizedMessage, *, watch_until: str) -> None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
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

    def close_task_for_owner_takeover(self, task_id: int) -> None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
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

    def get_task_by_id(self, task_id: int) -> TaskRecord:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return _task_from_row(row)

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

    def get_active_tasks_by_thread(self, chat_id: str, thread_id: str, *, now: str) -> list[TaskRecord]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE chat_id = ?
                  AND thread_id = ?
                  AND status IN ('watching', 'waiting_approval')
                  AND (watch_until IS NULL OR watch_until > ?)
                ORDER BY updated_at DESC, id DESC
                """,
                (chat_id, thread_id, now),
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

    def record_agent_message_for_task(self, task_id: int, message: NormalizedMessage) -> None:
        self.migrate()
        now = utc_now_iso()
        with self.connect() as conn:
            self._add_task_message(conn, task_id, message.message_id, "agent_reply", now)
            self._add_watch_keys(conn, task_id, _agent_reply_watch_keys_for_message(message), now)

    def list_active_watch_targets(self, *, now: str) -> list[dict[str, str | None]]:
        self.migrate()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT chat_id, chat_type, thread_id
                FROM tasks
                WHERE status IN ('watching', 'waiting_approval')
                  AND (watch_until IS NULL OR watch_until > ?)
                  AND chat_id IS NOT NULL
                ORDER BY chat_id, thread_id
                """,
                (now,),
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

    def count_pending_actions(self) -> int:
        self.migrate()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM actions WHERE status IN ('pending', 'sending')"
            ).fetchone()
        return int(row["count"])

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

    def _unique_short_id(self, prefix: str, seed: str) -> str:
        base = f"{prefix}_{sha256(seed.encode('utf-8')).hexdigest()[:8]}"
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT short_id FROM tasks WHERE short_id = ?",
                (base,),
            ).fetchone()
            if existing is None:
                return base
            for suffix in range(2, 100):
                candidate = f"{base}_{suffix}"
                existing = conn.execute(
                    "SELECT short_id FROM tasks WHERE short_id = ?",
                    (candidate,),
                ).fetchone()
                if existing is None:
                    return candidate
        raise RuntimeError("could not allocate task short id")

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
    )


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
