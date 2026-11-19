from __future__ import annotations

import json
import os
import shlex
import socket
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from .store.sqlite_store import SQLiteStore
from .time_utils import shift_instant

GROUP_MAIN_KEY = "__group_main__"
NATIVE_LOCATING_KEY = "__native_locating__"
SUBAGENT_EVENT_PREFIX = "internal:subagent:"
CACHED_HISTORY_EVENT_ID = "internal:history"


@dataclass(frozen=True)
class CoworkerEvent:
    id: int
    app_id: str
    chat_id: str
    topic_key: str
    message_id: str
    group_activation_id: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class CoworkerSession:
    id: int
    backend_session_id: str | None
    agent_working_dir: str


class CoworkerStore:
    """Durable ingress and provider-session state for one coworker group."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    def runtime_conflicts(self, *, app_id: str, chat_id: str, native: bool) -> int:
        """Require the previous runtime's uncertain work to be resolved before switching."""
        self.store.initialize()
        with self.store.connect() as conn:
            events = conn.execute(
                """SELECT COUNT(*) FROM coworker_events WHERE app_id = ? AND runtime = ?
                   AND (? = 1 OR chat_id = ?) AND status IN ('queued', 'processing', 'needs_review')""",
                (app_id, "legacy" if native else "native", int(not native), chat_id),
            ).fetchone()[0]
            other_runtime = "legacy" if native else "native"
            children = conn.execute(
                """SELECT COUNT(*) FROM coworker_subagents child
                   JOIN coworker_sessions s ON s.id = child.session_id
                   JOIN coworker_events origin ON origin.id = child.origin_event_id
                   WHERE s.app_id = ? AND (? = 1 OR s.chat_id = ?) AND origin.runtime = ?
                     AND child.status IN ('running', 'needs_review')""",
                (app_id, int(not native), chat_id, other_runtime),
            ).fetchone()[0]
            pending_actions = conn.execute(
                """SELECT COUNT(*) FROM actions a JOIN tasks t ON t.id = a.task_id
                   WHERE a.status IN ('pending', 'sending', 'failed_needs_review')
                     AND EXISTS (SELECT 1 FROM coworker_events e WHERE e.app_id = ?
                       AND (? = 1 OR e.chat_id = ?) AND e.runtime = ?
                       AND (e.message_id = t.root_message_id OR e.message_id = a.source_message_id))""",
                (app_id, int(not native), chat_id, other_runtime),
            ).fetchone()[0]
            pending_approvals = conn.execute(
                """SELECT COUNT(*) FROM approvals a JOIN tasks t ON t.id = a.task_id
                   WHERE a.status = 'pending' AND EXISTS (SELECT 1 FROM coworker_events e
                     WHERE e.app_id = ? AND (? = 1 OR e.chat_id = ?) AND e.runtime = ?
                       AND (e.message_id = t.root_message_id OR e.message_id = a.source_message_id))""",
                (app_id, int(not native), chat_id, other_runtime),
            ).fetchone()[0]
            if native:
                return (
                    int(events)
                    + int(children)
                    + int(pending_actions)
                    + int(pending_approvals)
                )
            deliveries = conn.execute(
                """SELECT COUNT(*) FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE e.app_id = ? AND d.status IN ('pending', 'sending', 'needs_review')""",
                (app_id,),
            ).fetchone()[0]
            handoffs = conn.execute(
                """SELECT COUNT(*) FROM coworker_native_handoffs h
                   JOIN coworker_sessions s ON s.id = h.session_id WHERE s.app_id = ?""",
                (app_id,),
            ).fetchone()[0]
            return (
                int(events)
                + int(children)
                + int(pending_actions)
                + int(pending_approvals)
                + int(deliveries)
                + int(handoffs)
            )

    def enqueue(
        self,
        *,
        app_id: str,
        bot_open_id: str,
        raw: dict[str, Any],
        group_activation_id: int | None = None,
        chat_layout: str = "topic",
        native: bool = False,
    ) -> bool:
        event = _mapping(raw.get("event"))
        message = _mapping(event.get("message"))
        header = _mapping(raw.get("header"))
        message_id = _required_string(message, "message_id")
        chat_id = _required_string(message, "chat_id")
        if not app_id or not bot_open_id:
            raise ValueError("app and bot identity must be resolved before enqueue")
        aliases = _topic_aliases(message)
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            # The Feishu long connection expects a callback within three seconds.
            conn.execute("PRAGMA busy_timeout = 1500")
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id, chat_id, event_id, group_activation_id, status FROM coworker_events WHERE app_id = ? AND message_id = ? AND revision = 1",
                (app_id, message_id),
            ).fetchone()
            cache_only = (
                existing is not None
                and existing["chat_id"] == chat_id
                and existing["status"] == "completed"
                and (
                    existing["event_id"] == CACHED_HISTORY_EVENT_ID
                    or (
                        existing["event_id"] is None
                        and existing["group_activation_id"] is None
                    )
                )
            )
            if existing is not None and not cache_only:
                return False
            if native:
                topic_key = f"{NATIVE_LOCATING_KEY}:{message_id}"
            elif chat_layout == "group" and not message.get("thread_id"):
                topic_key = GROUP_MAIN_KEY
            elif chat_layout in {"group", "topic"}:
                topic_key = self._resolve_topic(conn, app_id, chat_id, aliases)
            else:
                raise ValueError("unknown coworker chat layout")
            status = "queued" if topic_key else "needs_review"
            error = None if topic_key else "unresolved_or_ambiguous_topic"
            topic_key = topic_key or f"unresolved:{message_id}"
            runtime: dict[str, Any] = {"agent_started": False, "retry_count": 0}
            parent = message.get("parent_id") or message.get("reply_to_message_id")
            if native and isinstance(parent, str) and parent:
                origin = conn.execute(
                    """SELECT origin.id FROM coworker_events origin
                       WHERE origin.app_id = ? AND origin.chat_id = ?
                         AND origin.runtime = 'native' AND origin.status = 'processing' AND (
                           origin.message_id = ? OR CASE WHEN json_valid(origin.raw_json) THEN
                             ? IN (json_extract(origin.raw_json, '$._internal.source_message_id'),
                                   json_extract(origin.raw_json, '$._internal.reply_target'))
                             ELSE 0 END OR EXISTS (
                               SELECT 1 FROM coworker_native_steers steer
                               JOIN coworker_events supplement ON supplement.id = steer.event_id
                               WHERE steer.origin_event_id = origin.id AND steer.status = 'accepted'
                                 AND supplement.message_id = ?) OR EXISTS (
                               SELECT 1 FROM coworker_events supplement
                               WHERE supplement.app_id = origin.app_id AND supplement.chat_id = origin.chat_id
                                 AND supplement.message_id = ? AND supplement.status = 'queued'
                                 AND CASE WHEN json_valid(supplement.raw_json) THEN
                                   json_extract(supplement.raw_json, '$._runtime.followup_origin') = origin.id
                                   ELSE 0 END) OR EXISTS (
                               SELECT 1 FROM coworker_native_delivery_context c
                               WHERE c.delivery_id = CASE WHEN json_valid(origin.raw_json)
                                 THEN json_extract(origin.raw_json, '$._internal.delivery_id') END
                                 AND c.message_id = ?))
                       ORDER BY origin.id LIMIT 1""",
                    (app_id, chat_id, parent, parent, parent, parent, parent),
                ).fetchone()
                if origin is not None:
                    runtime.update(
                        followup_origin=int(origin["id"]), followup_parent=parent
                    )
            payload = json.dumps(
                {
                    "event": raw,
                    "bot_open_id": bot_open_id,
                    "_runtime": runtime,
                },
                ensure_ascii=False,
            )
            event_id = header.get("event_id") or f"internal:live:{message_id}"
            if cache_only and existing is not None:
                cursor = conn.execute(
                    """UPDATE coworker_events SET event_id = ?, topic_key = ?, group_activation_id = ?, runtime = ?,
                              raw_json = ?, status = ?, error = ?, received_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        event_id,
                        topic_key,
                        group_activation_id,
                        "native" if native else "legacy",
                        payload,
                        status,
                        error,
                        now,
                        now,
                        existing["id"],
                    ),
                )
                return cursor.rowcount == 1
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO coworker_events(
                  app_id, event_id, message_id, chat_id, topic_key, group_activation_id, runtime, raw_json,
                  status, error, received_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    event_id,
                    message_id,
                    chat_id,
                    topic_key,
                    group_activation_id,
                    "native" if native else "legacy",
                    payload,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        self.store.initialize()
        with self.store.connect() as conn:
            sessions = conn.execute(
                "SELECT id, app_id, chat_id, thread_id, provider, generation, state, updated_at FROM coworker_sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            events = conn.execute(
                "SELECT id, app_id, chat_id, topic_key, message_id, status, error, received_at, updated_at FROM coworker_events WHERE status != 'completed' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            delegations = conn.execute(
                "SELECT id, session_id, peer_id, status, deadline, updated_at FROM coworker_delegations WHERE status != 'completed' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            gaps = conn.execute(
                "SELECT app_id, chat_id, topic_key, needs_backfill, has_more, last_error, updated_at FROM coworker_history_state WHERE needs_backfill = 1 OR has_more = 1 ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            subagents = conn.execute(
                """
                SELECT s.child_thread_id, s.parent_thread_id, s.status, s.outcome,
                       s.started_at, s.updated_at, c.chat_id, c.thread_id AS topic_key
                FROM coworker_subagents s JOIN coworker_sessions c ON c.id = s.session_id
                WHERE s.status != 'completed' ORDER BY s.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            deliveries = conn.execute(
                """SELECT d.id, d.event_id, d.session_id, d.target_message_id,
                          d.status, d.sent_message_id, d.thread_id, d.error, d.updated_at,
                          e.app_id, e.chat_id, e.topic_key
                   FROM coworker_native_deliveries d JOIN coworker_events e ON e.id = d.event_id
                   ORDER BY d.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            handoffs = conn.execute(
                """SELECT h.session_id, h.status, h.error, h.updated_at, s.app_id, s.chat_id,
                          s.thread_id AS topic_key, s.backend_session_id, s.agent_working_dir
                   FROM coworker_native_handoffs h JOIN coworker_sessions s ON s.id = h.session_id
                   ORDER BY h.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            watches = conn.execute(
                """SELECT app_id, chat_id, topic_key, owner_open_id, expires_at, updated_at
                   FROM coworker_native_watches ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            steers = conn.execute(
                """SELECT s.event_id, s.session_id, s.turn_id,
                          CASE WHEN s.status = 'needs_review' AND e.status = 'discarded'
                               THEN 'discarded' ELSE s.status END AS status, s.updated_at
                   FROM coworker_native_steers s JOIN coworker_events e ON e.id = s.event_id
                   ORDER BY s.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return {
            "sessions": [dict(row) for row in sessions],
            "events": [dict(row) for row in events],
            "delegations": [dict(row) for row in delegations],
            "history_gaps": [dict(row) for row in gaps],
            "subagents": [dict(row) for row in subagents],
            "native_deliveries": [dict(row) for row in deliveries],
            "native_handoffs": [
                {
                    **dict(row),
                    "operator_command": (
                        f"cd {shlex.quote(str(row['agent_working_dir']))} && codex resume "
                        f"{shlex.quote(str(row['backend_session_id']))}"
                        if row["status"] == "ready"
                        else None
                    ),
                }
                for row in handoffs
            ],
            "native_watches": [dict(row) for row in watches],
            "native_steers": [dict(row) for row in steers],
            "memory_maintenance": self.store.get_checkpoint(
                "coworker.memory_maintenance"
            ),
        }

    def discard_event(self, event_id: int, *, daemon_stopped: bool) -> None:
        if not daemon_stopped:
            raise ValueError("stop the daemon before resolving an uncertain event")
        self.store.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT raw_json FROM coworker_events WHERE id = ?", (event_id,)
            ).fetchone()
            cursor = conn.execute(
                """UPDATE coworker_events SET status = 'discarded', raw_json = NULL,
                          error = 'discarded_by_operator', updated_at = ?
                   WHERE id = ? AND (status IN ('processing', 'needs_review') OR
                         (status = 'queued' AND message_id LIKE 'internal:subagent:%'))""",
                (self.store.clock(), event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("event is not awaiting operator review")
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_remove(raw_json, '$._runtime.batch_leader')
                   WHERE status = 'queued' AND json_valid(raw_json)
                     AND json_extract(raw_json, '$._runtime.batch_leader') = ?""",
                (event_id,),
            )
            if row is not None and row["raw_json"]:
                try:
                    internal = _mapping(
                        _mapping(json.loads(row["raw_json"])).get("_internal")
                    )
                except (TypeError, ValueError):
                    internal = {}
                child = internal.get("child_thread_id")
                if isinstance(child, str):
                    conn.execute(
                        "UPDATE coworker_subagents SET status = 'completed', outcome = 'resolved_by_operator', updated_at = ? WHERE child_thread_id = ? AND status = 'needs_review'",
                        (self.store.clock(), child),
                    )

    def reset_session(self, session_id: int, *, daemon_stopped: bool) -> None:
        if not daemon_stopped:
            raise ValueError("stop the daemon before resetting a coworker session")
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT * FROM coworker_sessions WHERE id = ? AND state = 'active'",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("active coworker session not found")
            if conn.execute(
                """SELECT 1 FROM coworker_events
                   WHERE app_id = ? AND chat_id = ? AND topic_key = ? AND (
                     status IN ('processing', 'needs_review') OR
                     (status = 'queued' AND (
                       message_id LIKE 'internal:subagent:%' OR
                       message_id LIKE 'internal:transfer:%' OR
                       CASE WHEN json_valid(raw_json)
                            THEN json_extract(raw_json, '$._runtime.steer_state') IS NOT NULL
                            ELSE 1 END
                     ))
                   )""",
                (session["app_id"], session["chat_id"], session["thread_id"]),
            ).fetchone():
                raise ValueError("resolve outstanding topic events before resetting")
            if conn.execute(
                "SELECT 1 FROM coworker_delegations WHERE session_id = ? AND status = 'pending'",
                (session_id,),
            ).fetchone():
                raise ValueError("resolve pending delegations before resetting")
            if conn.execute(
                "SELECT 1 FROM coworker_subagents WHERE session_id = ? AND status = 'running'",
                (session_id,),
            ).fetchone():
                raise ValueError("wait for running subagents before resetting")
            if conn.execute(
                """SELECT 1 FROM coworker_native_deliveries WHERE session_id = ?
                   AND status IN ('pending', 'sending', 'needs_review')""",
                (session_id,),
            ).fetchone():
                raise ValueError("resolve native deliveries before resetting")
            if conn.execute(
                "SELECT 1 FROM coworker_native_handoffs WHERE session_id = ?",
                (session_id,),
            ).fetchone():
                raise ValueError("complete the handoff before resetting")
            conn.execute(
                "UPDATE coworker_sessions SET state = 'closed', updated_at = ? WHERE id = ?",
                (self.store.clock(), session_id),
            )

    def _resolve_topic(
        self,
        conn: sqlite3.Connection,
        app_id: str,
        chat_id: str,
        aliases: list[str],
    ) -> str | None:
        if not aliases:
            return None
        keys: set[str] = set()
        for alias in aliases:
            row = conn.execute(
                "SELECT topic_key FROM coworker_topic_aliases WHERE app_id = ? AND chat_id = ? AND alias = ?",
                (app_id, chat_id, alias),
            ).fetchone()
            if row:
                keys.add(str(row["topic_key"]))
        if len(keys) > 1:
            return None
        topic_key = next(iter(keys), aliases[0])
        for alias in aliases:
            conn.execute(
                "INSERT OR IGNORE INTO coworker_topic_aliases(app_id, chat_id, alias, topic_key) VALUES (?, ?, ?, ?)",
                (app_id, chat_id, alias, topic_key),
            )
        return topic_key

    def resolve_native_topic(
        self, *, app_id: str, chat_id: str, aliases: list[str]
    ) -> str:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            topic = self._resolve_topic(conn, app_id, chat_id, aliases)
            if topic is None:
                raise ValueError("topic aliases are missing or ambiguous")
            return topic

    def claim_next(
        self,
        *,
        app_id: str | None = None,
        chat_id: str | None = None,
        internal_only: bool = False,
        native: bool = False,
    ) -> CoworkerEvent | None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Invalid queued payloads cannot be replayed. Quarantine them rather
            # than allowing JSON functions to stall every topic in the worker.
            conn.execute(
                """
                UPDATE coworker_events SET status = 'needs_review',
                  error = 'invalid_event_payload', updated_at = ?
                WHERE status = 'queued' AND (? IS NULL OR app_id = ?)
                  AND (? IS NULL OR chat_id = ?)
                  AND CASE WHEN json_valid(raw_json) THEN json_type(raw_json) != 'object' ELSE 1 END
                """,
                (self.store.clock(), app_id, app_id, chat_id, chat_id),
            )
            row = conn.execute(
                """
                SELECT e.* FROM coworker_events e
                WHERE e.status = 'queued'
                  AND e.runtime = ?
                  AND (? IS NULL OR e.app_id = ?)
                  AND (? IS NULL OR e.chat_id = ?)
                  AND (? = 0 OR e.message_id LIKE 'internal:subagent:%')
                  AND json_extract(e.raw_json, '$._runtime.steer_state') IS NULL
                  AND json_extract(e.raw_json, '$._runtime.batch_leader') IS NULL
                  AND (? = 0 OR NOT EXISTS (
                    SELECT 1 FROM coworker_native_deliveries d
                    JOIN coworker_sessions s ON s.id = d.session_id
                    WHERE s.app_id = e.app_id AND s.chat_id = e.chat_id
                      AND s.thread_id = e.topic_key
                      AND d.status IN ('pending', 'sending', 'needs_review')
                  ))
                  AND (? = 0 OR NOT EXISTS (
                    SELECT 1 FROM coworker_native_handoffs h
                    JOIN coworker_sessions s ON s.id = h.session_id
                    WHERE s.app_id = e.app_id AND s.chat_id = e.chat_id AND s.thread_id = e.topic_key
                      AND (h.status != 'pause_requested' OR e.message_id NOT LIKE 'internal:subagent:%')
                  ))
                  AND (? = 0 OR e.topic_key = '__group_main__' OR e.topic_key GLOB '__native_locating__:*' OR EXISTS (
                    SELECT 1 FROM coworker_sessions established WHERE established.app_id = e.app_id
                      AND established.chat_id = e.chat_id AND established.thread_id = e.topic_key
                      AND established.provider = 'codex' AND established.state = 'active'
                  ) OR NOT EXISTS (
                    SELECT 1 FROM coworker_native_deliveries opening
                    JOIN coworker_events origin ON origin.id = opening.event_id
                    WHERE origin.app_id = e.app_id AND origin.chat_id = e.chat_id
                      AND opening.purpose IN ('topic', 'transfer') AND opening.status IN ('pending', 'sending', 'needs_review')
                  ))
                  AND (json_extract(e.raw_json, '$._runtime.retry_after') IS NULL
                       OR julianday(json_extract(e.raw_json, '$._runtime.retry_after')) <= julianday(?))
                  AND NOT EXISTS (
                    SELECT 1 FROM coworker_events current
                    WHERE current.app_id = e.app_id AND current.chat_id = e.chat_id
                      AND current.topic_key = e.topic_key
                      AND (current.status = 'processing' OR (
                        current.status = 'needs_review'
                        AND CASE WHEN json_valid(current.raw_json)
                          THEN COALESCE(json_extract(current.raw_json, '$._runtime.agent_started'), 1)
                          ELSE 1 END = 1
                      ))
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM coworker_events earlier
                    WHERE earlier.app_id = e.app_id AND earlier.chat_id = e.chat_id
                      AND earlier.topic_key = e.topic_key AND earlier.status = 'queued'
                      AND (
                        (e.message_id LIKE 'internal:transfer:%' AND earlier.message_id LIKE 'internal:transfer:%'
                          AND (earlier.received_at < e.received_at OR
                            (earlier.received_at = e.received_at AND earlier.id < e.id)))
                        OR (e.message_id NOT LIKE 'internal:transfer:%' AND (
                          earlier.message_id LIKE 'internal:transfer:%' OR earlier.received_at < e.received_at OR
                          (earlier.received_at = e.received_at AND earlier.id < e.id)))
                      )
                      AND NOT (? = 1 AND e.message_id LIKE 'internal:subagent:%'
                        AND earlier.message_id NOT LIKE 'internal:subagent:%' AND EXISTS (
                          SELECT 1 FROM coworker_native_handoffs h
                          JOIN coworker_sessions s ON s.id = h.session_id
                          WHERE s.app_id = e.app_id AND s.chat_id = e.chat_id
                            AND s.thread_id = e.topic_key AND h.status = 'pause_requested'
                        ))
                  )
                ORDER BY e.received_at, e.id LIMIT 1
                """,
                (
                    "native" if native else "legacy",
                    app_id,
                    app_id,
                    chat_id,
                    chat_id,
                    int(internal_only),
                    int(native),
                    int(native),
                    int(native),
                    self.store.clock(),
                    int(native),
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE coworker_events SET status = 'processing', updated_at = ?,
                  raw_json = json_set(
                                      CASE WHEN json_type(raw_json, '$._runtime') = 'object'
                                           THEN raw_json
                                           ELSE json_set(raw_json, '$._runtime', json('{}')) END,
                                      '$._runtime.agent_started', json('false'),
                                      '$._runtime.owner_pid', ?, '$._runtime.owner_host', ?)
                WHERE id = ?
                """,
                (self.store.clock(), os.getpid(), socket.gethostname(), row["id"]),
            )
        raw = json.loads(str(row["raw_json"]))
        if not isinstance(raw, dict):
            raise ValueError("coworker event payload is not an object")
        return CoworkerEvent(
            id=int(row["id"]),
            app_id=str(row["app_id"]),
            chat_id=str(row["chat_id"]),
            topic_key=str(row["topic_key"]),
            message_id=str(row["message_id"]),
            group_activation_id=row["group_activation_id"],
            raw=cast(dict[str, Any], raw),
        )

    def mark_agent_started(self, event_id: int) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                "UPDATE coworker_events SET raw_json = json_set(raw_json, '$._runtime.agent_started', json('true')), updated_at = ? WHERE id = ? AND status = 'processing'",
                (self.store.clock(), event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("coworker event is no longer processing")

    def route_native_event(self, event: CoworkerEvent, topic_key: str) -> bool:
        """Move a claimed event after its actual Feishu location has been read."""
        if not topic_key or topic_key.startswith(NATIVE_LOCATING_KEY):
            raise ValueError("invalid native topic")
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT received_at FROM coworker_events WHERE id = ?
                   AND status = 'processing' AND runtime = 'native' AND topic_key = ?""",
                (event.id, event.topic_key),
            ).fetchone()
            if row is None:
                raise ValueError("native event is no longer being located")
            unresolved = conn.execute(
                """SELECT raw_json FROM coworker_events WHERE app_id = ? AND chat_id = ?
                   AND runtime = 'native' AND status IN ('queued', 'processing')
                   AND substr(topic_key, 1, ?) = ? AND id != ? AND (received_at < ? OR
                     (received_at = ? AND id < ?))""",
                (
                    event.app_id,
                    event.chat_id,
                    len(NATIVE_LOCATING_KEY) + 1,
                    f"{NATIVE_LOCATING_KEY}:",
                    event.id,
                    row["received_at"],
                    row["received_at"],
                    event.id,
                ),
            ).fetchall()
            for earlier in unresolved:
                try:
                    payload = _mapping(json.loads(str(earlier["raw_json"])))
                    envelope = _mapping(_mapping(payload.get("event")).get("event"))
                    message = _mapping(envelope.get("message"))
                    aliases = _topic_aliases(message)
                    hinted_thread = message.get("thread_id")
                    if (
                        topic_key.startswith("__private__:")
                        and not message.get("parent_id")
                        and not message.get("reply_to_message_id")
                        and topic_key != f"__private__:{message.get('message_id')}"
                    ):
                        continue
                    matches = any(
                        alias == topic_key
                        or conn.execute(
                            """SELECT 1 FROM coworker_topic_aliases
                               WHERE app_id = ? AND chat_id = ? AND alias = ? AND topic_key = ?""",
                            (event.app_id, event.chat_id, alias, topic_key),
                        ).fetchone()
                        is not None
                        for alias in aliases
                    )
                    if topic_key != GROUP_MAIN_KEY and hinted_thread and not matches:
                        continue
                except (TypeError, ValueError):
                    pass
                # Until the earlier message can be located, it may share this topic.
                conn.execute(
                    """UPDATE coworker_events SET status = 'queued',
                       raw_json = json_set(raw_json, '$._runtime.retry_after', ?), updated_at = ?
                       WHERE id = ?""",
                    (
                        shift_instant(self.store.clock(), delta=timedelta(seconds=1)),
                        self.store.clock(),
                        event.id,
                    ),
                )
                return False
            blocked = conn.execute(
                """SELECT 1 FROM coworker_events WHERE app_id = ? AND chat_id = ?
                   AND topic_key = ? AND id != ? AND (
                     status = 'processing' OR
                     (status = 'needs_review' AND CASE WHEN json_valid(raw_json)
                       THEN COALESCE(json_extract(raw_json, '$._runtime.agent_started'), 1)
                       ELSE 1 END = 1) OR
                     (status = 'queued' AND (message_id LIKE 'internal:transfer:%' OR received_at < ? OR
                       (received_at = ? AND id < ?)))
                   ) LIMIT 1""",
                (
                    event.app_id,
                    event.chat_id,
                    topic_key,
                    event.id,
                    row["received_at"],
                    row["received_at"],
                    event.id,
                ),
            ).fetchone()
            if blocked is None:
                blocked = conn.execute(
                    """SELECT 1 FROM coworker_native_deliveries d
                       JOIN coworker_sessions s ON s.id = d.session_id
                       WHERE s.app_id = ? AND s.chat_id = ? AND s.thread_id = ?
                         AND d.status IN ('pending', 'sending', 'needs_review') LIMIT 1""",
                    (event.app_id, event.chat_id, topic_key),
                ).fetchone()
            established = conn.execute(
                """SELECT 1 FROM coworker_sessions WHERE app_id = ? AND chat_id = ?
                   AND thread_id = ? AND provider = 'codex' AND state = 'active'""",
                (event.app_id, event.chat_id, topic_key),
            ).fetchone()
            if blocked is None and topic_key != GROUP_MAIN_KEY and established is None:
                blocked = conn.execute(
                    """SELECT 1 FROM coworker_native_deliveries d
                       JOIN coworker_events e ON e.id = d.event_id
                       WHERE e.app_id = ? AND e.chat_id = ? AND d.purpose IN ('topic', 'transfer')
                         AND d.status IN ('pending', 'sending', 'needs_review') LIMIT 1""",
                    (event.app_id, event.chat_id),
                ).fetchone()
            if blocked is None:
                blocked = conn.execute(
                    """SELECT 1 FROM coworker_native_handoffs h
                       JOIN coworker_sessions s ON s.id = h.session_id
                       WHERE s.app_id = ? AND s.chat_id = ? AND s.thread_id = ? LIMIT 1""",
                    (event.app_id, event.chat_id, topic_key),
                ).fetchone()
            conn.execute(
                "UPDATE coworker_events SET topic_key = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    topic_key,
                    "queued" if blocked else "processing",
                    self.store.clock(),
                    event.id,
                ),
            )
            return blocked is None

    def retry_before_agent(
        self, event_id: int, *, error: str, steer: bool = False
    ) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT raw_json FROM coworker_events WHERE id = ? AND status = ?",
                (event_id, "queued" if steer else "processing"),
            ).fetchone()
            if row is None:
                raise ValueError("coworker event is no longer processing")
            payload = _mapping(json.loads(str(row["raw_json"])))
            runtime = _mapping(payload.get("_runtime"))
            if runtime.get("agent_started") is not False:
                raise ValueError(
                    "cannot retry an event after agent execution may have started"
                )
            if steer:
                runtime.pop("steer_state", None)
            attempts = int(runtime.get("retry_count") or 0) + 1
            runtime["retry_count"] = attempts
            status = "queued" if attempts <= 3 else "needs_review"
            runtime["retry_after"] = (
                shift_instant(
                    self.store.clock(),
                    delta=timedelta(seconds=(2, 10, 60)[attempts - 1]),
                )
                if attempts <= 3
                else None
            )
            payload["_runtime"] = runtime
            conn.execute(
                "UPDATE coworker_events SET status = ?, raw_json = ?, error = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    error[:500],
                    self.store.clock(),
                    event_id,
                ),
            )

    def recover_interrupted(self, *, app_id: str, chat_id: str) -> None:
        """Recover only claims whose same-host owner is known to have exited."""
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_remove(raw_json, '$._runtime.batch_leader')
                   WHERE app_id = ? AND chat_id = ? AND status = 'queued' AND json_valid(raw_json)
                     AND json_extract(raw_json, '$._runtime.batch_leader') IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM coworker_events leader
                       WHERE leader.id = json_extract(coworker_events.raw_json, '$._runtime.batch_leader')
                         AND leader.status = 'processing')""",
                (app_id, chat_id),
            )
            rows = conn.execute(
                "SELECT id, raw_json FROM coworker_events WHERE app_id = ? AND chat_id = ? AND status = 'processing'",
                (app_id, chat_id),
            ).fetchall()
            for row in rows:
                try:
                    payload = _mapping(json.loads(str(row["raw_json"])))
                    runtime = _mapping(payload.get("_runtime"))
                except (TypeError, ValueError):
                    continue
                if not _owner_has_exited(runtime):
                    continue
                started = runtime.get("agent_started") is not False
                status = "needs_review" if started else "queued"
                error = (
                    "interrupted_agent_outcome_unknown"
                    if started
                    else "interrupted_before_agent"
                )
                conn.execute(
                    "UPDATE coworker_events SET status = ?, error = ?, updated_at = ? WHERE id = ? AND status = 'processing'",
                    (status, error, self.store.clock(), row["id"]),
                )
                conn.execute(
                    """UPDATE coworker_events SET raw_json = json_remove(raw_json, '$._runtime.batch_leader')
                       WHERE app_id = ? AND chat_id = ? AND status = 'queued' AND json_valid(raw_json)
                         AND json_extract(raw_json, '$._runtime.batch_leader') = ?""",
                    (app_id, chat_id, row["id"]),
                )
            steers = conn.execute(
                """SELECT id, raw_json FROM coworker_events
                   WHERE app_id = ? AND chat_id = ? AND status = 'queued'
                     AND json_valid(raw_json)
                     AND json_extract(raw_json, '$._runtime.steer_state') IS NOT NULL""",
                (app_id, chat_id),
            ).fetchall()
            for row in steers:
                payload = _mapping(json.loads(str(row["raw_json"])))
                runtime = _mapping(payload.get("_runtime"))
                if not _owner_has_exited(runtime):
                    continue
                sending = runtime.get("steer_state") == "sending"
                runtime.pop("steer_state", None)
                runtime["agent_started"] = sending
                payload["_runtime"] = runtime
                conn.execute(
                    "UPDATE coworker_events SET status = ?, raw_json = ?, error = ?, updated_at = ? WHERE id = ?",
                    (
                        "needs_review" if sending else "queued",
                        json.dumps(payload, ensure_ascii=False),
                        "interrupted_steer_outcome_unknown"
                        if sending
                        else "interrupted_before_steer",
                        self.store.clock(),
                        row["id"],
                    ),
                )
            children = conn.execute(
                """SELECT s.*, c.app_id, c.chat_id, c.thread_id AS topic_key,
                          COALESCE(original.message_id, e.message_id) AS source_message_id,
                          COALESCE(t.source_event_id, e.id) AS source_event_id,
                          COALESCE(t.reply_target, e.message_id) AS reply_target,
                          t.delivery_id AS transfer_delivery_id, e.group_activation_id,
                          e.runtime AS origin_runtime
                   FROM coworker_subagents s
                   JOIN coworker_sessions c ON c.id = s.session_id
                   JOIN coworker_events e ON e.id = s.origin_event_id
                   LEFT JOIN coworker_native_transfers t ON t.event_id = e.id
                   LEFT JOIN coworker_events original ON original.id = t.source_event_id
                   WHERE c.app_id = ? AND c.chat_id = ? AND s.status = 'running'""",
                (app_id, chat_id),
            ).fetchall()
            for row in children:
                if not _owner_has_exited(
                    {"owner_pid": row["owner_pid"], "owner_host": row["owner_host"]}
                ):
                    continue
                child_id = str(row["child_thread_id"])
                turn_id = str(row["child_turn_id"] or "unknown")
                raw = {
                    "_internal": {
                        "kind": "subagent_completed",
                        "child_thread_id": child_id,
                        "agent_path": row["agent_path"],
                        "child_turn_id": turn_id,
                        "origin_event_id": row["origin_event_id"],
                        "source_event_id": row["source_event_id"],
                        "source_message_id": row["source_message_id"],
                        "reply_target": row["reply_target"],
                        "delivery_id": row["transfer_delivery_id"],
                        "outcome": "interrupted_unknown",
                    },
                    "bot_open_id": row["bot_open_id"],
                    "_runtime": {"agent_started": True, "retry_count": 0},
                }
                now = self.store.clock()
                conn.execute(
                    "UPDATE coworker_subagents SET status = 'needs_review', outcome = 'interrupted_unknown', updated_at = ? WHERE child_thread_id = ?",
                    (now, child_id),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO coworker_events(
                         app_id, message_id, chat_id, topic_key, group_activation_id, runtime,
                         raw_json, status, error, received_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'needs_review', 'subagent_outcome_unknown', ?, ?)""",
                    (
                        row["app_id"],
                        f"{SUBAGENT_EVENT_PREFIX}{child_id}:{turn_id}",
                        row["chat_id"],
                        row["topic_key"],
                        row["group_activation_id"],
                        row["origin_runtime"],
                        json.dumps(raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def claim_next_steer(
        self, *, app_id: str, chat_id: str | None, native: bool = False
    ) -> CoworkerEvent | None:
        """Claim a follow-up without starting a second turn for its topic."""
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT e.* FROM coworker_events e
                WHERE e.app_id = ? AND (? IS NULL OR e.chat_id = ?) AND e.status = 'queued'
                  AND e.runtime = ?
                  AND e.message_id NOT LIKE 'internal:subagent:%'
                  AND e.message_id NOT LIKE 'internal:transfer:%'
                  AND json_valid(e.raw_json)
                  AND json_extract(e.raw_json, '$._runtime.steer_state') IS NULL
                  AND json_extract(e.raw_json, '$._runtime.batch_leader') IS NULL
                  AND (? = 0 OR COALESCE(json_extract(e.raw_json, '$._runtime.steer_checked'), 0) = 0)
                  AND (? = 0 OR NOT EXISTS (
                    SELECT 1 FROM coworker_native_handoffs h JOIN coworker_sessions s ON s.id = h.session_id
                    WHERE s.app_id = e.app_id AND s.chat_id = e.chat_id AND s.thread_id = e.topic_key
                  ))
                  AND (json_extract(e.raw_json, '$._runtime.retry_after') IS NULL
                       OR julianday(json_extract(e.raw_json, '$._runtime.retry_after')) <= julianday(?))
                  AND EXISTS (SELECT 1 FROM coworker_events current
                    WHERE current.app_id = e.app_id AND current.chat_id = e.chat_id
                      AND current.topic_key = e.topic_key AND current.status = 'processing')
                  AND NOT EXISTS (SELECT 1 FROM coworker_events earlier
                    WHERE earlier.app_id = e.app_id AND earlier.chat_id = e.chat_id
                      AND earlier.topic_key = e.topic_key AND earlier.status = 'queued'
                      AND earlier.message_id NOT LIKE 'internal:subagent:%'
                      AND (? = 0 OR CASE WHEN json_valid(earlier.raw_json)
                        THEN COALESCE(json_extract(earlier.raw_json, '$._runtime.steer_checked'), 0) = 0
                        ELSE 1 END)
                      AND (earlier.received_at < e.received_at OR
                           (earlier.received_at = e.received_at AND earlier.id < e.id)))
                ORDER BY e.received_at, e.id LIMIT 1
                """,
                (
                    app_id,
                    chat_id,
                    chat_id,
                    "native" if native else "legacy",
                    int(native),
                    int(native),
                    self.store.clock(),
                    int(native),
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(raw_json,
                   '$._runtime.steer_state', 'preparing', '$._runtime.owner_pid', ?,
                   '$._runtime.owner_host', ?), updated_at = ? WHERE id = ?""",
                (os.getpid(), socket.gethostname(), self.store.clock(), row["id"]),
            )
        raw = json.loads(str(row["raw_json"]))
        if not isinstance(raw, dict):
            raise ValueError("coworker event payload is not an object")
        return CoworkerEvent(
            id=int(row["id"]),
            app_id=str(row["app_id"]),
            chat_id=str(row["chat_id"]),
            topic_key=str(row["topic_key"]),
            message_id=str(row["message_id"]),
            group_activation_id=row["group_activation_id"],
            raw=cast(dict[str, Any], raw),
        )

    def mark_steer_sending(self, event_id: int, *, native: bool = False) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(raw_json,
                   '$._runtime.steer_state', 'sending', '$._runtime.agent_started', json('true')),
                   updated_at = ? WHERE id = ? AND status = 'queued'
                   AND json_extract(raw_json, '$._runtime.steer_state') = 'preparing'
                   AND (? = 0 OR NOT EXISTS (
                     SELECT 1 FROM coworker_native_handoffs h JOIN coworker_sessions s ON s.id = h.session_id
                     WHERE s.app_id = coworker_events.app_id AND s.chat_id = coworker_events.chat_id
                       AND s.thread_id = coworker_events.topic_key
                   ))""",
                (self.store.clock(), event_id, int(native)),
            )
            if cursor.rowcount != 1:
                raise ValueError("coworker steer is no longer preparing")

    def skip_native_steer(self, event_id: int) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(
                     json_remove(raw_json, '$._runtime.steer_state'), '$._runtime.steer_checked', json('true')),
                     updated_at = ? WHERE id = ? AND status = 'queued'""",
                (self.store.clock(), event_id),
            )

    def associate_native_followup(
        self, event_id: int, origin_event_id: int, parent_id: str
    ) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(raw_json,
                     '$._runtime.followup_origin', ?, '$._runtime.followup_parent', ?), updated_at = ?
                   WHERE id = ? AND status = 'queued' AND EXISTS (
                     SELECT 1 FROM coworker_events origin WHERE origin.id = ?
                       AND origin.runtime = 'native'
                       AND origin.app_id = coworker_events.app_id
                       AND origin.chat_id = coworker_events.chat_id
                       AND origin.topic_key = coworker_events.topic_key)""",
                (
                    origin_event_id,
                    parent_id,
                    self.store.clock(),
                    event_id,
                    origin_event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("follow-up no longer belongs to the active task")

    def defer_late_native_steer(self, event_id: int, origin_event_id: int) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.store.clock()
            origin = conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(raw_json,
                     '$._runtime.superseded', json('true')), updated_at = ?
                   WHERE id = ? AND status = 'processing' AND runtime = 'native'
                     AND EXISTS (SELECT 1 FROM coworker_events followup WHERE followup.id = ?
                       AND followup.status = 'queued' AND followup.app_id = coworker_events.app_id
                       AND followup.chat_id = coworker_events.chat_id
                       AND followup.topic_key = coworker_events.topic_key)""",
                (now, origin_event_id, event_id),
            )
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_set(
                     json_remove(raw_json, '$._runtime.steer_state'),
                     '$._runtime.steer_checked', json('true'),
                     '$._runtime.agent_started', json('false'),
                     '$._runtime.previous_unpublished', json(?)), updated_at = ?
                   WHERE id = ? AND status = 'queued'""",
                ("true" if origin.rowcount == 1 else "false", now, event_id),
            )

    def finish_steer(
        self, event_id: int, *, retry: bool = False, error: str | None = None
    ) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            if retry:
                conn.execute(
                    """UPDATE coworker_events SET raw_json = json_set(
                       json_remove(raw_json, '$._runtime.steer_state'),
                       '$._runtime.agent_started', json('false')), updated_at = ?
                       WHERE id = ? AND status = 'queued'""",
                    (self.store.clock(), event_id),
                )
            else:
                conn.execute(
                    """UPDATE coworker_events SET status = ?, error = ?,
                       raw_json = CASE WHEN ? IS NULL THEN NULL ELSE raw_json END,
                       updated_at = ? WHERE id = ? AND status = 'queued'""",
                    (
                        "completed" if error is None else "needs_review",
                        error,
                        error,
                        self.store.clock(),
                        event_id,
                    ),
                )

    def register_subagent(
        self,
        *,
        child_thread_id: str,
        parent_thread_id: str,
        agent_path: str,
        session_id: int,
        origin_event_id: int,
        bot_open_id: str,
    ) -> None:
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO coworker_subagents(
                  child_thread_id, parent_thread_id, agent_path, session_id, origin_event_id,
                  bot_open_id, status, owner_pid, owner_host, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    child_thread_id,
                    parent_thread_id,
                    agent_path,
                    session_id,
                    origin_event_id,
                    bot_open_id,
                    os.getpid(),
                    socket.gethostname(),
                    now,
                    now,
                ),
            )

    def subagent_context(self, thread_id: str) -> dict[str, Any] | None:
        self.store.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM coworker_subagents WHERE child_thread_id = ?",
                (thread_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def rebind_completed_subagent(
        self,
        *,
        child_thread_id: str,
        parent_thread_id: str,
        session_id: int,
        origin_event_id: int,
        bot_open_id: str,
    ) -> bool:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_subagents SET session_id = ?, origin_event_id = ?,
                   bot_open_id = ?, child_turn_id = NULL, outcome = NULL, status = 'running',
                   owner_pid = ?, owner_host = ?, started_at = ?, updated_at = ?
                   WHERE child_thread_id = ? AND parent_thread_id = ? AND status = 'completed'""",
                (
                    session_id,
                    origin_event_id,
                    bot_open_id,
                    os.getpid(),
                    socket.gethostname(),
                    self.store.clock(),
                    self.store.clock(),
                    child_thread_id,
                    parent_thread_id,
                ),
            )
            return cursor.rowcount == 1

    def rebind_running_subagent(
        self,
        *,
        child_thread_id: str,
        parent_thread_id: str,
        session_id: int,
        origin_event_id: int,
        bot_open_id: str,
    ) -> bool:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_subagents SET origin_event_id = ?, bot_open_id = ?, updated_at = ?
                   WHERE child_thread_id = ? AND parent_thread_id = ? AND session_id = ? AND status = 'running'""",
                (
                    origin_event_id,
                    bot_open_id,
                    self.store.clock(),
                    child_thread_id,
                    parent_thread_id,
                    session_id,
                ),
            )
            return cursor.rowcount == 1

    def record_subagent_turn(
        self, child_thread_id: str, turn_id: str, *, outcome: str | None = None
    ) -> bool:
        self.store.initialize()
        with self.store.connect() as conn:
            if outcome is None:
                cursor = conn.execute(
                    """UPDATE coworker_subagents SET child_turn_id = ?, status = 'running',
                       outcome = NULL, updated_at = ? WHERE child_thread_id = ?
                       AND (child_turn_id IS NULL OR child_turn_id != ? OR status = 'running')""",
                    (turn_id, self.store.clock(), child_thread_id, turn_id),
                )
            else:
                cursor = conn.execute(
                    """UPDATE coworker_subagents SET outcome = ?, updated_at = ?
                       WHERE child_thread_id = ? AND child_turn_id = ?""",
                    (outcome, self.store.clock(), child_thread_id, turn_id),
                )
        return cursor.rowcount == 1

    def complete_subagent(self, child_thread_id: str) -> bool:
        """Atomically record completion and enqueue one wake for the root topic."""
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT s.*, c.app_id, c.chat_id, c.thread_id AS topic_key,
                          c.backend_session_id, COALESCE(original.message_id, e.message_id) AS source_message_id,
                          COALESCE(t.source_event_id, e.id) AS source_event_id,
                          COALESCE(t.reply_target, e.message_id) AS reply_target,
                          t.delivery_id AS transfer_delivery_id, e.group_activation_id, e.runtime AS origin_runtime
                   FROM coworker_subagents s
                   JOIN coworker_sessions c ON c.id = s.session_id
                   JOIN coworker_events e ON e.id = s.origin_event_id
                   LEFT JOIN coworker_native_transfers t ON t.event_id = e.id
                   LEFT JOIN coworker_events original ON original.id = t.source_event_id
                   WHERE s.child_thread_id = ?""",
                (child_thread_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            conn.execute(
                "UPDATE coworker_subagents SET status = 'completed', updated_at = ? WHERE child_thread_id = ?",
                (now, child_thread_id),
            )
            # Nested workers report to their immediate parent; that parent is
            # responsible for incorporating the result into the root session.
            if row["parent_thread_id"] != row["backend_session_id"]:
                return False
            turn_id = str(row["child_turn_id"] or "unknown")
            message_id = f"{SUBAGENT_EVENT_PREFIX}{child_thread_id}:{turn_id}"
            raw = {
                "_internal": {
                    "kind": "subagent_completed",
                    "child_thread_id": child_thread_id,
                    "agent_path": row["agent_path"],
                    "child_turn_id": turn_id,
                    "origin_event_id": int(row["origin_event_id"]),
                    "source_event_id": int(row["source_event_id"]),
                    "source_message_id": row["source_message_id"],
                    "reply_target": row["reply_target"],
                    "delivery_id": row["transfer_delivery_id"],
                    "outcome": row["outcome"] or "unknown",
                },
                "bot_open_id": row["bot_open_id"],
                "_runtime": {"agent_started": False, "retry_count": 0},
            }
            cursor = conn.execute(
                """INSERT OR IGNORE INTO coworker_events(
                     app_id, message_id, chat_id, topic_key, group_activation_id, runtime,
                     raw_json, status, received_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    row["app_id"],
                    message_id,
                    row["chat_id"],
                    row["topic_key"],
                    row["group_activation_id"],
                    row["origin_runtime"],
                    json.dumps(raw, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def running_subagents(
        self, *, app_id: str, chat_id: str | None
    ) -> list[dict[str, Any]]:
        self.store.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT s.* FROM coworker_subagents s
                   JOIN coworker_sessions c ON c.id = s.session_id
                   WHERE c.app_id = ? AND (? IS NULL OR c.chat_id = ?) AND s.status = 'running'""",
                (app_id, chat_id, chat_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_subagent_uncertain(self, child_thread_id: str) -> bool:
        """Quarantine a child whose final outcome cannot be observed."""
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT s.*, c.app_id, c.chat_id, c.thread_id AS topic_key,
                          COALESCE(original.message_id, e.message_id) AS source_message_id,
                          COALESCE(t.source_event_id, e.id) AS source_event_id,
                          COALESCE(t.reply_target, e.message_id) AS reply_target,
                          t.delivery_id AS transfer_delivery_id, e.group_activation_id, e.runtime AS origin_runtime
                   FROM coworker_subagents s JOIN coworker_sessions c ON c.id = s.session_id
                   JOIN coworker_events e ON e.id = s.origin_event_id
                   LEFT JOIN coworker_native_transfers t ON t.event_id = e.id
                   LEFT JOIN coworker_events original ON original.id = t.source_event_id
                   WHERE s.child_thread_id = ? AND s.status = 'running'""",
                (child_thread_id,),
            ).fetchone()
            if row is None:
                return False
            turn_id = str(row["child_turn_id"] or "unknown")
            raw = {
                "_internal": {
                    "kind": "subagent_completed",
                    "child_thread_id": child_thread_id,
                    "agent_path": row["agent_path"],
                    "child_turn_id": turn_id,
                    "origin_event_id": row["origin_event_id"],
                    "source_event_id": row["source_event_id"],
                    "source_message_id": row["source_message_id"],
                    "reply_target": row["reply_target"],
                    "delivery_id": row["transfer_delivery_id"],
                    "outcome": "interrupted_unknown",
                },
                "bot_open_id": row["bot_open_id"],
                "_runtime": {"agent_started": True, "retry_count": 0},
            }
            conn.execute(
                """UPDATE coworker_subagents SET status = 'needs_review',
                   outcome = 'interrupted_unknown', updated_at = ? WHERE child_thread_id = ?""",
                (now, child_thread_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO coworker_events(
                     app_id, message_id, chat_id, topic_key, group_activation_id, runtime,
                     raw_json, status, error, received_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'needs_review', 'subagent_outcome_unknown', ?, ?)""",
                (
                    row["app_id"],
                    f"{SUBAGENT_EVENT_PREFIX}{child_thread_id}:{turn_id}",
                    row["chat_id"],
                    row["topic_key"],
                    row["group_activation_id"],
                    row["origin_runtime"],
                    json.dumps(raw, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return True

    def finish_event(self, event_id: int, *, error: str | None = None) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE coworker_events SET status = ?, error = ?,
                  raw_json = CASE WHEN ? IS NULL THEN NULL ELSE raw_json END, updated_at = ?
                WHERE id = ? AND status = 'processing'
                """,
                (
                    "completed" if error is None else "needs_review",
                    error,
                    error,
                    self.store.clock(),
                    event_id,
                ),
            )

    def defer_event(self, event_id: int) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE coworker_events SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'processing'",
                (self.store.clock(), event_id),
            )

    def defer_event_until(self, event_id: int, retry_after: str) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_events SET status = 'queued',
                   raw_json = json_set(raw_json, '$._runtime.retry_after', ?), updated_at = ?
                   WHERE id = ? AND status = 'processing'""",
                (retry_after, self.store.clock(), event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("event is no longer processing")

    def scrub_expired_payloads(
        self, *, cutoff: str, dry_run: bool, protect_pending: bool = False
    ) -> int:
        self.store.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM coworker_events WHERE raw_json IS NOT NULL AND status != 'processing' AND julianday(received_at) < julianday(?) AND (? = 0 OR status NOT IN ('queued', 'needs_review'))",
                (cutoff, int(protect_pending)),
            ).fetchall()
            if not dry_run:
                for row in rows:
                    conn.execute(
                        "UPDATE coworker_events SET raw_json = NULL, status = 'needs_review', error = 'content_expired', updated_at = ? WHERE id = ?",
                        (self.store.clock(), row["id"]),
                    )
        return len(rows)

    def match_delegation(
        self,
        *,
        app_id: str,
        chat_id: str,
        topic_key: str,
        peer_id: str,
        message_id: str,
        parent_id: str | None,
        text: str,
    ) -> tuple[str, int] | None:
        """Match an authorized peer only to a sent request or a stable token."""
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT d.id, d.hop, a.status AS action_status, a.result_json
                FROM coworker_delegations d
                JOIN coworker_sessions s ON s.id = d.session_id
                JOIN actions a ON a.id = d.request_action_id
                WHERE s.app_id = ? AND s.chat_id = ? AND s.thread_id = ?
                  AND d.peer_id = ? AND (d.status = 'pending' OR
                    (d.status = 'completed' AND d.result_message_id = ?))
                ORDER BY d.created_at, d.id
                """,
                (app_id, chat_id, topic_key, peer_id, message_id),
            ).fetchall()
            for row in rows:
                delegation_id = str(row["id"])
                action_status = str(row["action_status"])
                contains_token = delegation_id in text
                result = _mapping(json.loads(row["result_json"] or "{}"))
                sent_message_id = result.get("sent_message_id")
                if action_status in {"pending", "sending"} and (
                    contains_token or (parent_id and parent_id == sent_message_id)
                ):
                    return "waiting", int(row["hop"])
                if action_status != "sent":
                    continue
                if not contains_token and (
                    not parent_id or parent_id != sent_message_id
                ):
                    continue
                conn.execute(
                    "UPDATE coworker_delegations SET status = 'completed', result_message_id = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                    (message_id, self.store.clock(), delegation_id),
                )
                return "matched", int(row["hop"])
        return None

    def get_or_create_session(
        self,
        *,
        app_id: str,
        chat_id: str,
        topic_key: str,
        provider: str,
        working_dir: str,
    ) -> CoworkerSession:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM coworker_sessions WHERE app_id = ? AND chat_id = ? AND thread_id = ? AND provider = ? AND state = 'active'",
                (app_id, chat_id, topic_key, provider),
            ).fetchone()
            if row is None:
                generation = conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM coworker_sessions WHERE app_id = ? AND chat_id = ? AND thread_id = ? AND provider = ?",
                    (app_id, chat_id, topic_key, provider),
                ).fetchone()[0]
                now = self.store.clock()
                cursor = conn.execute(
                    """
                    INSERT INTO coworker_sessions(app_id, chat_id, thread_id, provider, generation, agent_working_dir, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        app_id,
                        chat_id,
                        topic_key,
                        provider,
                        generation,
                        working_dir,
                        now,
                        now,
                    ),
                )
                return CoworkerSession(int(cursor.lastrowid or 0), None, working_dir)
            return CoworkerSession(
                int(row["id"]), row["backend_session_id"], str(row["agent_working_dir"])
            )

    def save_session_id(self, session_id: int, backend_session_id: str) -> None:
        if not backend_session_id:
            raise ValueError("backend session ID must not be empty")
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                "UPDATE coworker_sessions SET backend_session_id = ?, updated_at = ? WHERE id = ? AND state = 'active'",
                (backend_session_id, self.store.clock(), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("coworker session is no longer active")

    def has_session(
        self, *, app_id: str, chat_id: str, topic_key: str, provider: str
    ) -> bool:
        self.store.initialize()
        with self.store.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM coworker_sessions WHERE app_id = ? AND chat_id = ? AND thread_id = ? AND provider = ? AND state = 'active'",
                    (app_id, chat_id, topic_key, provider),
                ).fetchone()
                is not None
            )

    def history_state(
        self, *, app_id: str, chat_id: str, topic_key: str
    ) -> dict[str, Any] | None:
        self.store.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM coworker_history_state WHERE app_id = ? AND chat_id = ? AND topic_key = ?",
                (app_id, chat_id, topic_key),
            ).fetchone()
        return None if row is None else dict(row)

    def ensure_history_state(
        self, *, app_id: str, chat_id: str, topic_key: str
    ) -> dict[str, Any]:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO coworker_history_state(app_id, chat_id, topic_key, updated_at) VALUES (?, ?, ?, ?)",
                (app_id, chat_id, topic_key, self.store.clock()),
            )
        state = self.history_state(app_id=app_id, chat_id=chat_id, topic_key=topic_key)
        if state is None:
            raise RuntimeError("coworker history state could not be created")
        return state

    def set_history_state(
        self,
        *,
        app_id: str,
        chat_id: str,
        topic_key: str,
        needs_backfill: bool,
        has_more: bool | None,
        error: str | None,
        generation: int,
    ) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO coworker_history_state(app_id, chat_id, topic_key, needs_backfill, has_more, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_id, chat_id, topic_key) DO UPDATE SET
                  needs_backfill=CASE WHEN coworker_history_state.generation = ?
                                      THEN excluded.needs_backfill ELSE 1 END,
                  has_more=excluded.has_more,
                  last_error=excluded.last_error, updated_at=excluded.updated_at
                """,
                (
                    app_id,
                    chat_id,
                    topic_key,
                    int(needs_backfill),
                    None if has_more is None else int(has_more),
                    error,
                    self.store.clock(),
                    generation,
                ),
            )

    def mark_history_gaps(self, app_id: str) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE coworker_history_state SET needs_backfill = 1, generation = generation + 1, updated_at = ? WHERE app_id = ?",
                (self.store.clock(), app_id),
            )

    def cache_history_message(
        self, *, app_id: str, chat_id: str, topic_key: str, message_id: str
    ) -> None:
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO coworker_events(app_id, event_id, message_id, chat_id, topic_key, status, received_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    app_id,
                    CACHED_HISTORY_EVENT_ID,
                    message_id,
                    chat_id,
                    topic_key,
                    now,
                    now,
                ),
            )


def _mapping(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _owner_has_exited(runtime: dict[str, Any]) -> bool:
    if runtime.get("owner_host") != socket.gethostname():
        return False
    pid = runtime.get("owner_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"coworker event is missing {key}")
    return value


def _topic_aliases(message: dict[str, Any]) -> list[str]:
    aliases = [
        value
        for key in ("thread_id", "root_id")
        if isinstance((value := message.get(key)), str) and value
    ]
    # A top-level message can serve as its own root before a thread ID exists.
    if not message.get("root_id") and not message.get("parent_id"):
        message_id = message.get("message_id")
        if isinstance(message_id, str) and message_id:
            aliases.append(message_id)
    return list(dict.fromkeys(aliases))
