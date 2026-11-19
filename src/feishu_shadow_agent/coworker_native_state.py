from __future__ import annotations

import os
import socket
from datetime import timedelta
from typing import Any

from .coworker_store import (
    _associate_queued_replies,  # pyright: ignore[reportPrivateUsage]
    _owner_has_exited,  # pyright: ignore[reportPrivateUsage]
)
from .store.sqlite_store import SQLiteStore
from .time_utils import shift_instant


class NativeCoworkerState:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def require_dry_run_session(
        self, session_id: int, backend_session_id: str | None
    ) -> None:
        if backend_session_id is None:
            return
        self.store.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT backend_session_id FROM coworker_native_dry_run_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None or row[0] != backend_session_id:
                raise ValueError(
                    "existing session was not created with dry-run isolation"
                )

    def save_dry_run_session(self, session_id: int, backend_session_id: str) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO coworker_native_dry_run_sessions(session_id, backend_session_id)
                   VALUES (?, ?) ON CONFLICT(session_id) DO NOTHING""",
                (session_id, backend_session_id),
            )
        self.require_dry_run_session(session_id, backend_session_id)

    def set_watch(
        self,
        *,
        app_id: str,
        chat_id: str,
        topic_key: str,
        owner_open_id: str,
        duration_seconds: int,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("watch duration must be positive")
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO coworker_native_watches(app_id, chat_id, topic_key,
                   owner_open_id, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(app_id, chat_id, topic_key) DO UPDATE SET
                     owner_open_id = excluded.owner_open_id, expires_at = excluded.expires_at,
                     updated_at = excluded.updated_at""",
                (
                    app_id,
                    chat_id,
                    topic_key,
                    owner_open_id,
                    shift_instant(now, delta=timedelta(seconds=duration_seconds)),
                    now,
                ),
            )

    def remove_watch(self, *, app_id: str, chat_id: str, topic_key: str) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "DELETE FROM coworker_native_watches WHERE app_id = ? AND chat_id = ? AND topic_key = ?",
                (app_id, chat_id, topic_key),
            )

    def is_watched(
        self, *, app_id: str, chat_id: str, topic_key: str, owner_open_id: str
    ) -> bool:
        self.store.initialize()
        with self.store.connect() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM coworker_native_watches WHERE app_id = ? AND chat_id = ?
                   AND topic_key = ? AND owner_open_id = ? AND julianday(expires_at) > julianday(?)""",
                    (app_id, chat_id, topic_key, owner_open_id, self.store.clock()),
                ).fetchone()
                is not None
            )

    def request_handoff(
        self, session_id: int, *, resume: bool = False, retry: bool = False
    ) -> None:
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                """SELECT s.backend_session_id FROM coworker_sessions s
                   WHERE s.id = ? AND s.provider = 'codex' AND s.state = 'active'
                     AND EXISTS (SELECT 1 FROM coworker_events e WHERE e.app_id = s.app_id
                       AND e.chat_id = s.chat_id AND e.topic_key = s.thread_id AND e.runtime = 'native')""",
                (session_id,),
            ).fetchone()
            if session is None or not session["backend_session_id"]:
                raise ValueError("active Codex session is unavailable")
            if retry and resume:
                raise ValueError("retry applies to pause only")
            if retry:
                cursor = conn.execute(
                    """UPDATE coworker_native_handoffs SET status = 'pause_requested', error = NULL,
                       updated_at = ? WHERE session_id = ? AND status = 'needs_review'""",
                    (now, session_id),
                )
            elif resume:
                cursor = conn.execute(
                    """UPDATE coworker_native_handoffs SET status = 'resume_requested', error = NULL,
                       updated_at = ? WHERE session_id = ? AND status = 'ready'""",
                    (now, session_id),
                )
            else:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO coworker_native_handoffs(session_id, status, updated_at)
                       VALUES (?, 'pause_requested', ?)""",
                    (session_id, now),
                )
            if cursor.rowcount != 1:
                raise ValueError("session is not in the expected handoff state")

    def claim_handoff(self, *, app_id: str) -> dict[str, Any] | None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT h.session_id, h.status, s.backend_session_id, s.thread_id, s.agent_working_dir
                   FROM coworker_native_handoffs h JOIN coworker_sessions s ON s.id = h.session_id
                   WHERE s.app_id = ? AND s.state = 'active' AND s.backend_session_id IS NOT NULL
                     AND (h.status = 'resume_requested' OR (h.status = 'pause_requested'
                       AND NOT EXISTS (SELECT 1 FROM coworker_events e
                         WHERE e.app_id = s.app_id AND e.chat_id = s.chat_id AND e.topic_key = s.thread_id
                           AND (e.status = 'processing' OR (e.status = 'queued'
                             AND json_valid(e.raw_json) AND json_extract(e.raw_json, '$._runtime.steer_state') IS NOT NULL)
                             OR (e.status IN ('queued', 'needs_review')
                             AND e.message_id LIKE 'internal:subagent:%')))
                       AND NOT EXISTS (SELECT 1 FROM coworker_subagents child
                         WHERE child.session_id = s.id AND child.status IN ('running', 'needs_review'))
                       AND NOT EXISTS (SELECT 1 FROM coworker_native_deliveries d
                         WHERE d.session_id = s.id AND d.status IN ('pending', 'sending', 'needs_review'))))
                   ORDER BY h.updated_at, h.session_id LIMIT 1""",
                (app_id,),
            ).fetchone()
            if row is None:
                return None
            status = "releasing" if row["status"] == "pause_requested" else "reloading"
            conn.execute(
                """UPDATE coworker_native_handoffs SET status = ?, owner_pid = ?, owner_host = ?,
                   updated_at = ? WHERE session_id = ?""",
                (
                    status,
                    os.getpid(),
                    socket.gethostname(),
                    self.store.clock(),
                    row["session_id"],
                ),
            )
            result = dict(row)
            result["operation"] = status
            return result

    def finish_handoff(
        self, session_id: int, *, operation: str, error: str | None
    ) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            if error is None and operation == "reloading":
                cursor = conn.execute(
                    "DELETE FROM coworker_native_handoffs WHERE session_id = ? AND status = ?",
                    (session_id, operation),
                )
            else:
                cursor = conn.execute(
                    """UPDATE coworker_native_handoffs SET status = ?, error = ?, updated_at = ?
                       WHERE session_id = ? AND status = ?""",
                    (
                        "needs_review" if error else "ready",
                        error,
                        self.store.clock(),
                        session_id,
                        operation,
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("handoff is no longer in progress")

    def recover_interrupted_handoffs(self, *, app_id: str) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT h.session_id, h.owner_pid, h.owner_host FROM coworker_native_handoffs h
                   JOIN coworker_sessions s ON s.id = h.session_id
                   WHERE s.app_id = ? AND h.status IN ('releasing', 'reloading')""",
                (app_id,),
            ).fetchall()
            for row in rows:
                if _owner_has_exited(dict(row)):
                    conn.execute(
                        """UPDATE coworker_native_handoffs SET status = 'needs_review',
                           error = 'interrupted_handoff_outcome_unknown', updated_at = ?
                           WHERE session_id = ?""",
                        (self.store.clock(), row["session_id"]),
                    )

    def record_steer(
        self,
        *,
        event_id: int,
        origin_event_id: int,
        session_id: int,
        turn_id: str,
        source_revision: int,
        error: str | None,
    ) -> None:
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE coworker_events SET status = ?, error = ?,
                   raw_json = CASE WHEN ? IS NULL THEN NULL ELSE raw_json END, updated_at = ?
                   WHERE id = ? AND status = 'queued'
                     AND json_extract(raw_json, '$._runtime.steer_state') = 'sending'""",
                ("needs_review" if error else "completed", error, error, now, event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("steered event is no longer sending")
            conn.execute(
                """INSERT INTO coworker_native_steers(event_id, origin_event_id, session_id,
                   turn_id, source_revision, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    origin_event_id,
                    session_id,
                    turn_id,
                    source_revision,
                    "needs_review" if error else "accepted",
                    now,
                ),
            )
            if error is None:
                source = conn.execute(
                    "SELECT app_id, chat_id, message_id FROM coworker_events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if source is None:
                    raise ValueError("steered event is unavailable")
                _associate_queued_replies(
                    conn,
                    origin_event_id=origin_event_id,
                    app_id=str(source["app_id"]),
                    chat_id=str(source["chat_id"]),
                    parent_id=str(source["message_id"]),
                )
