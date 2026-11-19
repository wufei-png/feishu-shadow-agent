from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from feishu_shadow_agent.coworker_native_store import NativeDeliveryStore
from feishu_shadow_agent.coworker_store import CoworkerStore
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _event(store: SQLiteStore, *, message: str, topic: str) -> tuple[int, int]:
    store.initialize()
    with store.connect() as conn:
        session = conn.execute(
            """INSERT INTO coworker_sessions(app_id, chat_id, thread_id, provider,
               agent_working_dir, created_at, updated_at) VALUES ('app', 'chat', ?,
               'codex', '/tmp', 'now', 'now') RETURNING id""",
            (topic,),
        ).fetchone()[0]
        event = conn.execute(
            """INSERT INTO coworker_events(app_id, message_id, chat_id, topic_key,
               group_activation_id, raw_json, status, received_at, updated_at)
               VALUES ('app', ?, 'chat', ?, 1, '{}', 'processing', 'now', 'now')
               RETURNING id""",
            (message, topic),
        ).fetchone()[0]
    return int(event), int(session)


def test_reply_and_completed_event_are_committed_together(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    native = NativeDeliveryStore(store)
    event, session = _event(store, message="source", topic="topic")
    with pytest.raises(ValueError, match="not an internal event"):
        native.queue_reply(
            event_id=event,
            session_id=session,
            source_revision=1,
            target_message_id="other",
            text="answer",
        )
    with store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "processing"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM coworker_native_deliveries").fetchone()[
                0
            ]
            == 0
        )

    native.queue_reply(
        event_id=event,
        session_id=session,
        source_revision=1,
        target_message_id="source",
        text="answer",
    )
    with store.connect() as conn:
        assert tuple(
            conn.execute("SELECT status, raw_json FROM coworker_events").fetchone()
        ) == ("completed", None)
    delivery = native.claim(app_id="app", chat_id="chat")
    assert delivery is not None and delivery.text == "answer"
    assert native.claim(app_id="app", chat_id="chat") is None
    native.finish(delivery.id, status="sent", sent_message_id="reply")
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, sent_message_id, idempotency_key FROM coworker_native_deliveries"
        ).fetchone()
        assert tuple(row) == ("sent", "reply", delivery.idempotency_key)


def test_interrupted_send_needs_review_without_blocking_other_topics(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    native = NativeDeliveryStore(store)
    first_event, first_session = _event(store, message="one", topic="one")
    second_event, second_session = _event(store, message="two", topic="two")
    for event, session, message in (
        (first_event, first_session, "one"),
        (second_event, second_session, "two"),
    ):
        native.queue_reply(
            event_id=event,
            session_id=session,
            source_revision=1,
            target_message_id=message,
            text="answer",
        )
    first = native.claim(app_id="app", chat_id="chat")
    assert first is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE coworker_native_deliveries SET owner_pid = 2147483647, owner_host = ? WHERE id = ?",
            (socket.gethostname(), first.id),
        )
    native.recover_interrupted(app_id="app", chat_id="chat")
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries WHERE id = ?",
                (first.id,),
            ).fetchone()[0]
            == "needs_review"
        )
    second = native.claim(app_id="app", chat_id="chat")
    assert second is not None and second.event_id == second_event
    native.finish(second.id, status="previewed")
    assert native.claim(app_id="app", chat_id="chat") is None


def test_runtime_switch_requires_unfinished_previous_work_to_be_resolved(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    events = CoworkerStore(store)
    event, session = _event(store, message="source", topic="topic")
    assert events.runtime_conflicts(app_id="app", chat_id="chat", native=True) == 1
    with store.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET runtime = 'native' WHERE id = ?", (event,)
        )
    assert events.runtime_conflicts(app_id="app", chat_id="chat", native=True) == 0
    assert events.runtime_conflicts(app_id="app", chat_id="chat", native=False) == 1
    NativeDeliveryStore(store).queue_reply(
        event_id=event,
        session_id=session,
        source_revision=1,
        target_message_id="source",
        text="answer",
    )
    assert events.runtime_conflicts(app_id="app", chat_id="chat", native=False) == 1


@pytest.mark.parametrize("leader_finished", [False, True])
def test_interrupted_watch_batch_releases_reservations(
    tmp_path: Path, leader_finished: bool
) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    native, events = NativeDeliveryStore(store), CoworkerStore(store)
    leader, _session = _event(store, message="leader", topic="topic")
    with store.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET runtime='native', raw_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "_runtime": {
                            "owner_pid": 2147483647,
                            "owner_host": socket.gethostname(),
                            "agent_started": False,
                        }
                    }
                ),
                leader,
            ),
        )
        member = conn.execute(
            """INSERT INTO coworker_events(app_id, message_id, chat_id, topic_key, runtime,
               raw_json, status, received_at, updated_at)
               VALUES ('app', 'member', 'chat', 'topic', 'native', '{"_runtime":{}}',
                       'queued', 'now', 'now') RETURNING id"""
        ).fetchone()[0]
    assert native.reserve_watch_batch(leader, [(member, "member", 1)]) == 1
    assert events.claim_next_steer(app_id="app", chat_id="chat", native=True) is None
    if leader_finished:
        events.finish_event(leader, error="outcome unknown")
    events.recover_interrupted(app_id="app", chat_id="chat")
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, raw_json FROM coworker_events WHERE id=?", (leader,)
        ).fetchone()
        assert row["status"] == ("needs_review" if leader_finished else "queued")
        member_raw = conn.execute(
            "SELECT raw_json FROM coworker_events WHERE id=?", (member,)
        ).fetchone()[0]
        assert "batch_leader" not in json.loads(member_raw)["_runtime"]
