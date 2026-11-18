from __future__ import annotations

from pathlib import Path

import pytest

from feishu_shadow_agent.coworker_store import CoworkerStore
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _event(message_id: str, **fields: str) -> dict[str, object]:
    return {
        "header": {"event_id": f"ev_{message_id}"},
        "event": {"message": {"message_id": message_id, "chat_id": "chat_a", **fields}},
    }


def test_topic_aliases_and_claims_preserve_shared_session_and_serial_order(
    tmp_path: Path,
) -> None:
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    store = CoworkerStore(db)
    assert store.enqueue(app_id="app_a", bot_open_id="bot", raw=_event("root"))
    assert store.enqueue(
        app_id="app_a",
        bot_open_id="bot",
        raw=_event("reply", thread_id="thread", root_id="root", parent_id="root"),
    )
    assert not store.enqueue(
        app_id="app_a",
        bot_open_id="bot",
        raw=_event("reply", thread_id="thread", root_id="root"),
    )
    first = store.claim_next()
    assert first is not None and first.message_id == "root"
    assert store.claim_next() is None
    first_session = store.get_or_create_session(
        app_id="app_a",
        chat_id="chat_a",
        topic_key=first.topic_key,
        provider="codex",
        working_dir="/first",
    )
    store.save_session_id(first_session.id, "provider-session")
    store.finish_event(first.id)
    second = store.claim_next()
    assert second is not None and second.message_id == "reply"
    assert second.topic_key == first.topic_key
    second_session = store.get_or_create_session(
        app_id="app_a",
        chat_id="chat_a",
        topic_key=second.topic_key,
        provider="codex",
        working_dir="/changed",
    )
    assert second_session == type(first_session)(
        first_session.id, "provider-session", "/first"
    )


def test_unresolved_or_conflicting_topic_is_held_for_review(tmp_path: Path) -> None:
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    store = CoworkerStore(db)
    assert store.enqueue(
        app_id="app", bot_open_id="bot", raw=_event("orphan", parent_id="unknown")
    )
    assert store.claim_next() is None
    with db.connect() as conn:
        row = conn.execute("SELECT status, error FROM coworker_events").fetchone()
    assert tuple(row) == ("needs_review", "unresolved_or_ambiguous_topic")


def test_different_topics_can_be_claimed_independently(tmp_path: Path) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root_a"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root_b"))
    first = store.claim_next()
    second = store.claim_next()
    assert first is not None and second is not None
    assert first.topic_key != second.topic_key


def test_invalid_queued_event_is_quarantined_without_stalling_other_topics(
    tmp_path: Path,
) -> None:
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    store = CoworkerStore(db)
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("broken"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("healthy"))
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET raw_json = '{' WHERE message_id = 'broken'"
        )
    claimed = store.claim_next(app_id="app", chat_id="chat_a")
    assert claimed is not None and claimed.message_id == "healthy"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM coworker_events WHERE message_id = 'broken'"
        ).fetchone()
    assert tuple(row) == ("needs_review", "invalid_event_payload")


def test_recovery_distinguishes_pre_agent_and_uncertain_agent_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root_a"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root_b"))
    first = store.claim_next()
    second = store.claim_next()
    assert first is not None and second is not None
    store.mark_agent_started(second.id)
    monkeypatch.setattr(
        "feishu_shadow_agent.coworker_store._owner_has_exited", lambda _runtime: True
    )
    store.recover_interrupted(app_id="app", chat_id="chat_a")
    with store.store.connect() as conn:
        rows = {
            row["message_id"]: (row["status"], row["error"])
            for row in conn.execute(
                "SELECT message_id, status, error FROM coworker_events"
            )
        }
    assert rows["root_a"] == ("queued", "interrupted_before_agent")
    assert rows["root_b"] == ("needs_review", "interrupted_agent_outcome_unknown")
    resumed = store.claim_next()
    assert resumed is not None and resumed.message_id == "root_a"


def test_recovery_does_not_take_a_live_process_claim(tmp_path: Path) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root"))
    assert store.claim_next() is not None
    store.recover_interrupted(app_id="app", chat_id="chat_a")
    with store.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "processing"
        )


def test_exhausted_pre_agent_retries_do_not_block_later_topic_messages(
    tmp_path: Path,
) -> None:
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    store = CoworkerStore(db)
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root"))
    store.enqueue(
        app_id="app",
        bot_open_id="bot",
        raw=_event("reply", thread_id="thread", root_id="root", parent_id="root"),
    )
    for minute in range(4):
        db.clock = lambda minute=minute: f"2099-01-01T00:{minute:02d}:00+00:00"
        event = store.claim_next()
        assert event is not None and event.message_id == "root"
        store.retry_before_agent(event.id, error="temporary")
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id='root'"
            ).fetchone()[0]
            == "needs_review"
        )
    later = store.claim_next()
    assert later is not None and later.message_id == "reply"


def test_claim_next_can_scope_to_running_app_and_group(tmp_path: Path) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    store.enqueue(app_id="app_old", bot_open_id="bot", raw=_event("old"))
    store.enqueue(app_id="app_new", bot_open_id="bot", raw=_event("new"))
    selected = store.claim_next(app_id="app_new", chat_id="chat_a")
    assert selected is not None and selected.message_id == "new"
    assert store.claim_next(app_id="app_new", chat_id="other_chat") is None
    old = store.claim_next(app_id="app_old", chat_id="chat_a")
    assert old is not None and old.message_id == "old"


def test_regular_group_mentions_share_session_but_topic_is_separate(
    tmp_path: Path,
) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    store.enqueue(
        app_id="app", bot_open_id="bot", raw=_event("main_a"), chat_layout="group"
    )
    store.enqueue(
        app_id="app",
        bot_open_id="bot",
        raw=_event("main_b", parent_id="main_a"),
        chat_layout="group",
    )
    store.enqueue(
        app_id="app",
        bot_open_id="bot",
        raw=_event("thread_a", thread_id="omt_a", root_id="root_a"),
        chat_layout="group",
    )
    first = store.claim_next()
    second = store.claim_next()
    assert first is not None and second is not None
    assert first.message_id == "main_a" and second.message_id == "thread_a"
    assert first.topic_key != second.topic_key
    store.finish_event(first.id)
    third = store.claim_next()
    assert third is not None and third.message_id == "main_b"
    assert third.topic_key == first.topic_key


def test_uncertain_event_requires_explicit_discard_before_session_reset(
    tmp_path: Path,
) -> None:
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    store = CoworkerStore(db)
    store.enqueue(app_id="app", bot_open_id="bot", raw=_event("root"))
    event = store.claim_next()
    assert event is not None
    session = store.get_or_create_session(
        app_id="app",
        chat_id="chat_a",
        topic_key=event.topic_key,
        provider="codex",
        working_dir="/repo",
    )
    store.finish_event(event.id, error="uncertain")
    with pytest.raises(ValueError, match="resolve outstanding"):
        store.reset_session(session.id, daemon_stopped=True)
    with pytest.raises(ValueError, match="stop the daemon"):
        store.discard_event(event.id, daemon_stopped=False)
    store.discard_event(event.id, daemon_stopped=True)
    store.reset_session(session.id, daemon_stopped=True)
    next_session = store.get_or_create_session(
        app_id="app",
        chat_id="chat_a",
        topic_key=event.topic_key,
        provider="codex",
        working_dir="/repo",
    )
    assert next_session.id != session.id
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT generation FROM coworker_sessions WHERE id = ?",
                (next_session.id,),
            ).fetchone()[0]
            == 2
        )


def test_history_gap_is_not_lost_when_reconnect_races_backfill(tmp_path: Path) -> None:
    store = CoworkerStore(SQLiteStore(tmp_path / "agent.sqlite3"))
    state = store.ensure_history_state(app_id="app", chat_id="chat", topic_key="topic")
    store.mark_history_gaps("app")
    store.set_history_state(
        app_id="app",
        chat_id="chat",
        topic_key="topic",
        needs_backfill=False,
        has_more=False,
        error=None,
        generation=state["generation"],
    )
    after = store.history_state(app_id="app", chat_id="chat", topic_key="topic")
    assert after is not None and after["needs_backfill"] == 1
