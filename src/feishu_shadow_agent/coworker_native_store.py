from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from .coworker_store import _owner_has_exited  # pyright: ignore[reportPrivateUsage]
from .store.sqlite_store import SQLiteStore


@dataclass(frozen=True)
class NativeDelivery:
    id: int
    event_id: int
    session_id: int
    app_id: str
    chat_id: str
    topic_key: str
    activation_id: int | None
    purpose: str
    source_revision: int
    source_message_id: str
    target_message_id: str
    text: str
    reply_in_thread: bool
    requires_watch: bool
    idempotency_key: str
    context_sources: tuple[tuple[str, int], ...]


class NativeDeliveryStore:
    """Persist final answers separately from the legacy approval workflow."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    def reserve_watch_batch(
        self, event_id: int, batch: list[tuple[int, str, int]]
    ) -> int:
        reserved = 0
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for batch_id, message_id, _revision in batch:
                cursor = conn.execute(
                    """UPDATE coworker_events SET raw_json = json_set(raw_json, '$._runtime.batch_leader', ?)
                       WHERE id = ? AND message_id = ? AND status = 'queued' AND runtime = 'native'
                         AND json_extract(raw_json, '$._runtime.steer_state') IS NULL
                         AND json_extract(raw_json, '$._runtime.batch_leader') IS NULL
                         AND EXISTS (SELECT 1 FROM coworker_events leader WHERE leader.id = ?
                                     AND leader.status = 'processing'
                                     AND leader.app_id = coworker_events.app_id
                                     AND leader.chat_id = coworker_events.chat_id
                                     AND leader.topic_key = coworker_events.topic_key)""",
                    (event_id, batch_id, message_id, event_id),
                )
                if cursor.rowcount != 1:
                    break
                reserved += 1
        return reserved

    def release_watch_batch(self, event_id: int, *, app_id: str, chat_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE coworker_events SET raw_json = json_remove(raw_json, '$._runtime.batch_leader')
                   WHERE app_id = ? AND chat_id = ? AND status = 'queued' AND json_valid(raw_json)
                     AND json_extract(raw_json, '$._runtime.batch_leader') = ?""",
                (app_id, chat_id, event_id),
            )

    def queue_reply(
        self,
        *,
        event_id: int,
        session_id: int,
        source_revision: int,
        target_message_id: str,
        text: str,
        reply_in_thread: bool = False,
        source_event_id: int | None = None,
        batch: list[tuple[int, str, int]] | None = None,
        source_message_id: str | None = None,
        purpose: str = "reply",
        requires_watch: bool = False,
    ) -> None:
        if not text or not target_message_id:
            raise ValueError("a reply and its target are required")
        source_message_id = source_message_id or target_message_id
        if purpose not in {"reply", "topic", "transfer"}:
            raise ValueError("invalid delivery purpose")
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                """SELECT e.id, e.message_id, e.raw_json FROM coworker_events e
                   JOIN coworker_sessions s ON s.id = ? AND s.app_id = e.app_id
                     AND s.chat_id = e.chat_id AND s.thread_id = e.topic_key
                     AND s.state = 'active'
                   WHERE e.id = ? AND e.status = 'processing'""",
                (session_id, event_id),
            ).fetchone()
            if event is None:
                raise ValueError("reply does not belong to the processing session")
            payload_value: object = json.loads(str(event["raw_json"]))
            if not isinstance(payload_value, dict):
                raise ValueError("event payload is invalid")
            payload = cast(dict[str, Any], payload_value)
            bot_open_id = payload.get("bot_open_id")
            if event["message_id"] != source_message_id:
                try:
                    internal_value: object = payload["_internal"]
                except (ValueError, KeyError, TypeError):
                    raise ValueError("reply source is not an internal event") from None
                if (
                    not isinstance(internal_value, dict)
                    or cast(dict[str, Any], internal_value).get(
                        "source_event_id",
                        cast(dict[str, Any], internal_value).get("origin_event_id"),
                    )
                    != source_event_id
                ):
                    raise ValueError("reply origin does not match the internal event")
                internal = cast(dict[str, Any], internal_value)
                source = conn.execute(
                    """SELECT 1 FROM coworker_events source
                       JOIN coworker_events wake ON wake.id = ?
                       WHERE source.id = ? AND source.message_id = ?
                         AND source.app_id = wake.app_id AND source.chat_id = wake.chat_id
                         AND (source.topic_key = wake.topic_key OR ? = 1) AND source.runtime = 'native'""",
                    (
                        event_id,
                        source_event_id,
                        source_message_id,
                        int(
                            internal.get("kind") == "native_transfer"
                            or internal.get("delivery_id") is not None
                        ),
                    ),
                ).fetchone()
                if source is None:
                    raise ValueError("reply origin is not in the processing session")
            if source_message_id != target_message_id:
                internal_value = payload.get("_internal")
                if (
                    not isinstance(internal_value, dict)
                    or cast(dict[str, Any], internal_value).get("kind")
                    not in {"native_transfer", "subagent_completed"}
                    or cast(dict[str, Any], internal_value).get("reply_target")
                    != target_message_id
                ):
                    raise ValueError(
                        "reply target is not bound to the transferred topic"
                    )
                internal = cast(dict[str, Any], internal_value)
                original_delivery = conn.execute(
                    """SELECT 1 FROM coworker_native_deliveries d
                       JOIN coworker_events origin ON origin.id = d.event_id
                       JOIN coworker_events continuation ON continuation.id = ?
                       WHERE d.id = ? AND d.status = 'sent' AND d.purpose = 'transfer'
                         AND d.sent_message_id = ? AND d.bound_topic_key = continuation.topic_key
                         AND d.source_message_id = ? AND origin.app_id = continuation.app_id
                         AND origin.chat_id = continuation.chat_id""",
                    (
                        event_id,
                        internal.get("delivery_id"),
                        target_message_id,
                        source_message_id,
                    ),
                ).fetchone()
                if original_delivery is None:
                    raise ValueError("transferred reply target is not verified")
            cursor = conn.execute(
                """INSERT INTO coworker_native_deliveries(
                     event_id, session_id, purpose, source_revision, source_message_id,
                     target_message_id, text, reply_in_thread, requires_watch, bot_open_id,
                     idempotency_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    session_id,
                    purpose,
                    source_revision,
                    source_message_id,
                    target_message_id,
                    text,
                    int(reply_in_thread),
                    int(requires_watch),
                    bot_open_id,
                    str(uuid4()),
                    now,
                    now,
                ),
            )
            delivery_id = int(cursor.lastrowid or 0)
            self._finish_batch(conn, event_id, batch or [], now)
            context_sources = [
                (message_id, revision) for _, message_id, revision in batch or []
            ]
            steers = conn.execute(
                """SELECT e.message_id, s.source_revision, m.revision, m.is_deleted
                   FROM coworker_native_steers s JOIN coworker_events e ON e.id = s.event_id
                   LEFT JOIN messages m ON m.message_id = e.message_id
                   WHERE s.origin_event_id = ? AND s.session_id = ? AND s.status = 'accepted'""",
                (event_id, session_id),
            ).fetchall()
            for steer in steers:
                if steer["revision"] != steer["source_revision"] or steer["is_deleted"]:
                    raise ValueError("accepted steer source revision changed")
                context_sources.append(
                    (str(steer["message_id"]), int(steer["source_revision"]))
                )
            for message_id, revision in context_sources:
                conn.execute(
                    "INSERT INTO coworker_native_delivery_context(delivery_id, message_id, revision) VALUES (?, ?, ?)",
                    (delivery_id, message_id, revision),
                )
            conn.execute(
                """UPDATE coworker_events SET status = 'completed', raw_json = NULL,
                   error = NULL, updated_at = ? WHERE id = ?""",
                (now, event_id),
            )

    def finish_silent_batch(
        self, event_id: int, batch: list[tuple[int, str, int]]
    ) -> None:
        self.store.initialize()
        now = self.store.clock()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish_batch(conn, event_id, batch, now)
            cursor = conn.execute(
                """UPDATE coworker_events SET status = 'completed', raw_json = NULL,
                   error = NULL, updated_at = ? WHERE id = ? AND status = 'processing'""",
                (now, event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("batch event is no longer processing")

    @staticmethod
    def _finish_batch(
        conn: sqlite3.Connection,
        event_id: int,
        batch: list[tuple[int, str, int]],
        now: str,
    ) -> None:
        for batch_id, message_id, revision in batch:
            cursor = conn.execute(
                """UPDATE coworker_events SET status = 'completed', raw_json = NULL,
                   error = NULL, updated_at = ? WHERE id = ? AND message_id = ?
                     AND status = 'queued' AND runtime = 'native'
                     AND json_extract(raw_json, '$._runtime.batch_leader') = ? AND EXISTS (
                       SELECT 1 FROM coworker_events leader JOIN messages m ON m.message_id = ?
                       WHERE leader.id = ? AND leader.status = 'processing'
                         AND leader.app_id = coworker_events.app_id
                         AND leader.chat_id = coworker_events.chat_id
                         AND leader.topic_key = coworker_events.topic_key
                         AND m.revision = ? AND m.is_deleted = 0
                     )""",
                (now, batch_id, message_id, event_id, message_id, event_id, revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("watch batch changed during the agent turn")

    def claim(
        self, *, app_id: str, chat_id: str | None = None
    ) -> NativeDelivery | None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT d.*, e.app_id, e.chat_id, e.topic_key, e.group_activation_id
                   FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE d.status = 'pending' AND e.app_id = ? AND (? IS NULL OR e.chat_id = ?)
                     AND NOT EXISTS (
                       SELECT 1 FROM coworker_native_deliveries previous
                       WHERE previous.session_id = d.session_id
                         AND previous.id < d.id
                         AND previous.status IN ('pending', 'sending', 'needs_review')
                     )
                   ORDER BY d.id LIMIT 1""",
                (app_id, chat_id, chat_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE coworker_native_deliveries
                   SET status = 'sending', owner_pid = ?, owner_host = ?, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (os.getpid(), socket.gethostname(), self.store.clock(), row["id"]),
            )
            context = conn.execute(
                "SELECT message_id, revision FROM coworker_native_delivery_context WHERE delivery_id = ?",
                (row["id"],),
            ).fetchall()
        return NativeDelivery(
            id=int(row["id"]),
            event_id=int(row["event_id"]),
            session_id=int(row["session_id"]),
            app_id=str(row["app_id"]),
            chat_id=str(row["chat_id"]),
            topic_key=str(row["topic_key"]),
            activation_id=row["group_activation_id"],
            purpose=str(row["purpose"]),
            source_revision=int(row["source_revision"]),
            source_message_id=str(row["source_message_id"]),
            target_message_id=str(row["target_message_id"]),
            text=str(row["text"]),
            reply_in_thread=bool(row["reply_in_thread"]),
            requires_watch=bool(row["requires_watch"]),
            idempotency_key=str(row["idempotency_key"]),
            context_sources=tuple((str(item[0]), int(item[1])) for item in context),
        )

    def finish(
        self,
        delivery_id: int,
        *,
        status: str,
        sent_message_id: str | None = None,
        thread_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"sent", "previewed", "needs_review", "discarded"}:
            raise ValueError("invalid delivery result")
        if status == "sent" and not sent_message_id:
            raise ValueError("sent delivery needs a message receipt")
        self.store.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """UPDATE coworker_native_deliveries SET status = ?, sent_message_id = ?,
                   thread_id = ?, error = ?, updated_at = ? WHERE id = ? AND status = 'sending'""",
                (
                    status,
                    sent_message_id,
                    thread_id,
                    error,
                    self.store.clock(),
                    delivery_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("delivery is no longer being sent")

    def finish_topic(
        self, delivery_id: int, *, sent_message_id: str, thread_id: str
    ) -> None:
        if not sent_message_id or not thread_id:
            raise ValueError("topic receipt is incomplete")
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT d.*, e.app_id, e.chat_id, e.group_activation_id, e.received_at
                   FROM coworker_native_deliveries d JOIN coworker_events e ON e.id = d.event_id
                   WHERE d.id = ? AND d.status = 'sending' AND d.purpose IN ('topic', 'transfer')""",
                (delivery_id,),
            ).fetchone()
            if row is None or sent_message_id == row["target_message_id"]:
                raise ValueError("delivery does not have a valid topic receipt")
            alias = conn.execute(
                """SELECT topic_key FROM coworker_topic_aliases
                   WHERE app_id = ? AND chat_id = ? AND alias = ?""",
                (row["app_id"], row["chat_id"], thread_id),
            ).fetchone()
            topic_key = str(alias[0]) if alias else thread_id
            if topic_key == "__group_main__":
                raise ValueError("topic receipt resolves to the group main session")
            conn.execute(
                """INSERT OR IGNORE INTO coworker_topic_aliases(app_id, chat_id, alias, topic_key)
                   VALUES (?, ?, ?, ?)""",
                (row["app_id"], row["chat_id"], thread_id, topic_key),
            )
            now = self.store.clock()
            conn.execute(
                """UPDATE coworker_native_deliveries SET status = 'sent', sent_message_id = ?,
                   thread_id = ?, bound_topic_key = ?, error = NULL, updated_at = ? WHERE id = ?""",
                (sent_message_id, thread_id, topic_key, now, delivery_id),
            )
            if row["purpose"] == "transfer":
                if not row["bot_open_id"]:
                    raise ValueError("transfer bot identity is missing")
                payload = {
                    "_internal": {
                        "kind": "native_transfer",
                        "delivery_id": delivery_id,
                        "origin_event_id": row["event_id"],
                        "source_message_id": row["source_message_id"],
                        "reply_target": sent_message_id,
                    },
                    "bot_open_id": row["bot_open_id"],
                    "_runtime": {"agent_started": False, "retry_count": 0},
                }
                cursor = conn.execute(
                    """INSERT INTO coworker_events(app_id, message_id, chat_id, topic_key,
                         group_activation_id, runtime, raw_json, status, received_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'native', ?, 'queued', ?, ?)""",
                    (
                        row["app_id"],
                        f"internal:transfer:{delivery_id}",
                        row["chat_id"],
                        topic_key,
                        row["group_activation_id"],
                        json.dumps(payload, ensure_ascii=False),
                        row["received_at"],
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO coworker_native_transfers(event_id, delivery_id, source_event_id, reply_target) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, delivery_id, row["event_id"], sent_message_id),
                )

    def recover_interrupted(self, *, app_id: str, chat_id: str | None = None) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT d.id, d.owner_pid, d.owner_host
                   FROM coworker_native_deliveries d
                   JOIN coworker_events e ON e.id = d.event_id
                   WHERE d.status = 'sending' AND e.app_id = ? AND (? IS NULL OR e.chat_id = ?)""",
                (app_id, chat_id, chat_id),
            ).fetchall()
            for row in rows:
                if _owner_has_exited(dict(row)):
                    conn.execute(
                        """UPDATE coworker_native_deliveries SET status = 'needs_review',
                           error = 'interrupted_send_outcome_unknown', updated_at = ? WHERE id = ?""",
                        (self.store.clock(), row["id"]),
                    )

    def resolve_uncertain(
        self,
        delivery_id: int,
        *,
        sent_message_id: str | None,
        daemon_stopped: bool,
        thread_id: str | None = None,
    ) -> None:
        if not daemon_stopped:
            raise ValueError("stop the daemon before resolving a delivery")
        if sent_message_id is not None and not sent_message_id.strip():
            raise ValueError("message receipt cannot be empty")
        if thread_id is not None and (not thread_id.strip() or sent_message_id is None):
            raise ValueError("a topic thread requires a sent message receipt")
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT purpose, sent_message_id, target_message_id FROM coworker_native_deliveries WHERE id = ? AND status = 'needs_review'",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError("delivery is not awaiting review")
            if sent_message_id and (
                sent_message_id == row["target_message_id"]
                or (
                    row["sent_message_id"] and row["sent_message_id"] != sent_message_id
                )
            ):
                raise ValueError("receipt does not match the recorded delivery")
            topic = row["purpose"] in {"topic", "transfer"}
            if sent_message_id and topic and not thread_id:
                raise ValueError("a topic delivery also requires --thread-id")
            if thread_id and not topic:
                raise ValueError("this delivery did not open a topic")
            if sent_message_id and topic:
                conn.execute(
                    """UPDATE coworker_native_deliveries SET status = 'sending', owner_pid = ?,
                       owner_host = ?, updated_at = ? WHERE id = ?""",
                    (
                        os.getpid(),
                        socket.gethostname(),
                        self.store.clock(),
                        delivery_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """UPDATE coworker_native_deliveries SET status = ?, sent_message_id = ?,
                       error = NULL, updated_at = ? WHERE id = ? AND status = 'needs_review'""",
                    (
                        "sent" if sent_message_id else "discarded",
                        sent_message_id,
                        self.store.clock(),
                        delivery_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("delivery is not awaiting review")
        if sent_message_id and topic:
            if thread_id is None:
                raise ValueError("a topic delivery requires a thread ID")
            try:
                self.finish_topic(
                    delivery_id, sent_message_id=sent_message_id, thread_id=thread_id
                )
            except Exception:
                self.finish(
                    delivery_id,
                    status="needs_review",
                    sent_message_id=sent_message_id,
                    thread_id=thread_id,
                    error="topic_receipt_could_not_be_bound",
                )
                raise
