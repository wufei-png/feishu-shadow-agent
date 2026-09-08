from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

_KNOWN_TABLES = frozenset(
    {
        "messages",
        "approvals",
        "actions",
        "routing_audits",
        "agent_audits",
        "message_processing",
    }
)
_MESSAGE_PROCESSING_V3_SQL = """
CREATE TABLE message_processing_v3 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  task_id INTEGER,
  stage TEXT NOT NULL CHECK (stage IN ('task_router', 'task_session', 'resource_download')),
  status TEXT NOT NULL
    CHECK (status IN ('processed', 'processing_failed_terminal', 'blocked_waiting_external')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (message_id, revision, stage),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
)
"""
_ACTIVE_SEND_REPLY_INDEX_V3_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_active_send_reply_target
ON actions(
  task_id,
  target_message_id,
  execution_mode,
  COALESCE(source_message_id, ''),
  COALESCE(source_revision, 0)
)
WHERE kind = 'send_reply'
  AND status IN ('pending', 'sending', 'failed_needs_review')
  AND target_message_id IS NOT NULL
"""


def migrate_schema(
    conn: sqlite3.Connection, *, current_version: int, target_version: int
) -> None:
    """Upgrade a marked SQLite store between supported baselines.

    Empty databases use schema.sql directly. This path only handles in-place
    upgrades of an already-marked database, currently v2 -> v3.
    """

    if current_version == target_version:
        return
    if current_version == 2 and target_version == 3:
        _migrate_v2_to_v3(conn)
        conn.execute(f"PRAGMA user_version = {int(target_version)}")
        return
    raise RuntimeError(
        "SQLite database is not the current schema baseline; "
        "configure an empty database"
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "messages", "is_deleted INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "messages", "revision INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "messages", "semantic_hash TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "approvals", "source_message_id TEXT")
    _add_column_if_missing(conn, "approvals", "source_revision INTEGER")
    _add_column_if_missing(conn, "actions", "source_message_id TEXT")
    _add_column_if_missing(conn, "actions", "source_revision INTEGER")
    _add_column_if_missing(
        conn, "routing_audits", "revision INTEGER NOT NULL DEFAULT 1"
    )
    _add_column_if_missing(
        conn, "agent_audits", "input_message_revisions_json TEXT NOT NULL DEFAULT '[]'"
    )
    _add_column_if_missing(
        conn, "message_processing", "revision INTEGER NOT NULL DEFAULT 1"
    )
    _rebuild_message_processing_if_needed(conn)
    _backfill_action_source_bindings(conn)
    _backfill_approval_source_bindings(conn)
    conn.execute("DROP INDEX IF EXISTS idx_actions_active_send_reply_target")
    conn.execute(_ACTIVE_SEND_REPLY_INDEX_V3_SQL)
    conn.execute("DROP INDEX IF EXISTS idx_message_processing_message")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_processing_message
        ON message_processing(message_id, revision, stage)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_processing_status
        ON message_processing(status, updated_at)
        """
    )


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, definition: str
) -> None:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"refusing to alter unknown table {table}")
    column = definition.split()[0]
    if column in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"refusing to inspect unknown table {table}")
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _rebuild_message_processing_if_needed(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'message_processing'
        """
    ).fetchone()
    if row is None:
        return
    sql = " ".join(str(row["sql"]).split())
    if "UNIQUE (message_id, revision, stage)" in sql:
        return
    conn.execute(_MESSAGE_PROCESSING_V3_SQL)
    conn.execute(
        """
        INSERT INTO message_processing_v3(
          id, message_id, revision, task_id, stage, status, attempt_count,
          last_error, terminal_reason, created_at, updated_at
        )
        SELECT
          id, message_id, COALESCE(revision, 1), task_id, stage, status,
          attempt_count, last_error, terminal_reason, created_at, updated_at
        FROM message_processing
        """
    )
    conn.execute("DROP TABLE message_processing")
    conn.execute("ALTER TABLE message_processing_v3 RENAME TO message_processing")


def _backfill_action_source_bindings(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, task_id, target_message_id, payload_json,
               source_message_id, source_revision
        FROM actions
        WHERE kind = 'send_reply'
          AND (source_message_id IS NULL OR source_revision IS NULL)
        """
    ).fetchall()
    for row in rows:
        source_message_id, source_revision = _resolved_source_binding(
            conn,
            row,
            fallback_target=row["target_message_id"],
        )
        conn.execute(
            """
            UPDATE actions
            SET source_message_id = ?, source_revision = ?
            WHERE id = ?
            """,
            (source_message_id, source_revision, row["id"]),
        )


def _backfill_approval_source_bindings(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, task_id, payload_json, source_message_id, source_revision
        FROM approvals
        WHERE source_message_id IS NULL OR source_revision IS NULL
        """
    ).fetchall()
    for row in rows:
        source_message_id, source_revision = _resolved_source_binding(conn, row)
        conn.execute(
            """
            UPDATE approvals
            SET source_message_id = ?, source_revision = ?
            WHERE id = ?
            """,
            (source_message_id, source_revision, row["id"]),
        )


def _resolved_source_binding(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    fallback_target: Any = None,
) -> tuple[str | None, int]:
    payload = _loads_object(row["payload_json"])
    source_message_id = row["source_message_id"] or _payload_text(
        payload, "source_message_id"
    )
    if source_message_id is None:
        source_message_id = _single_external_task_message(conn, task_id=row["task_id"])
    if (
        source_message_id is None
        and isinstance(fallback_target, str)
        and fallback_target
    ):
        source_message_id = fallback_target
    if source_message_id is None:
        source_message_id = _payload_text(payload, "reply_target_message_id")
    source_revision = row["source_revision"]
    if source_revision is None:
        source_revision = _payload_revision(payload, "source_revision") or 1
    return source_message_id, int(source_revision)


def _single_external_task_message(
    conn: sqlite3.Connection, *, task_id: int | None
) -> str | None:
    if task_id is None:
        return None
    rows = conn.execute(
        """
        SELECT m.message_id
        FROM task_messages tm
        JOIN messages m ON m.message_id = tm.message_id
        WHERE tm.task_id = ?
          AND m.sender_role = 'external_user_message'
          AND m.is_deleted = 0
        """,
        (task_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    message_id = rows[0]["message_id"]
    return message_id if isinstance(message_id, str) and message_id else None


def _loads_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(loaded, dict):
        return cast(dict[str, Any], loaded)
    return {}


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _payload_revision(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        revision = int(value)
    except (TypeError, ValueError):
        return None
    return revision if revision > 0 else None


__all__ = ["migrate_schema"]
