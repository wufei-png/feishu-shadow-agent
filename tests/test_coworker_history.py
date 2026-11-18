from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from feishu_shadow_agent.coworker import event_message_from_raw
from feishu_shadow_agent.coworker_history import read_cached_history
from feishu_shadow_agent.coworker_store import CoworkerStore
from feishu_shadow_agent.message_normalization import MessageNormalizer
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _record(
    store: SQLiteStore, message_id: str, *, chat_id: str, thread_id: str, text: str
) -> None:
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": "group",
        "thread_id": thread_id,
        "message_type": "text",
        "content": json.dumps({"text": text}),
    }
    event = {
        "sender": {"sender_id": {"open_id": "alice"}, "sender_type": "user"},
        "message": message,
    }
    raw = {"header": {"event_id": message_id}, "event": event}
    assert CoworkerStore(store).enqueue(app_id="app", bot_open_id="bot", raw=raw)
    normalized = MessageNormalizer(owner_open_id="bot").normalize(
        event_message_from_raw(event, bot_id="bot")
    )
    store.upsert_message_with_revision(normalized)


def test_cached_history_supports_topic_search_and_pagination_without_crossing_groups(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    _record(store, "m1", chat_id="group", thread_id="topic-a", text="release Friday")
    _record(store, "m2", chat_id="group", thread_id="topic-b", text="release Monday")
    _record(store, "m3", chat_id="other", thread_id="topic-b", text="release private")
    first = read_cached_history(
        store.path, app_id="app", chat_id="group", query="release", limit=1
    )
    assert [item["message_id"] for item in first["messages"]] == ["m2"]
    assert first["has_more"] is True
    second = read_cached_history(
        store.path,
        app_id="app",
        chat_id="group",
        query="release",
        limit=1,
        before_event_id=first["next_before_event_id"],
    )
    assert [item["message_id"] for item in second["messages"]] == ["m1"]
    topic = read_cached_history(
        store.path, app_id="app", chat_id="group", topic_key="topic-a"
    )
    assert [item["message_id"] for item in topic["messages"]] == ["m1"]
    assert topic["coverage"] == "received_or_backfilled_messages_only"


def test_cache_cli_reads_existing_database_without_initializing_another_one(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    _record(store, "m1", chat_id="group", thread_id="topic-a", text="hello")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "feishu_shadow_agent.coworker_history",
            "--database",
            str(store.path),
            "--app-id",
            "app",
            "--chat-id",
            "group",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["messages"][0]["text"] == "hello"
    missing = tmp_path / "missing.sqlite3"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "feishu_shadow_agent.coworker_history",
            "--database",
            str(missing),
            "--app-id",
            "app",
            "--chat-id",
            "group",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and not missing.exists()


def test_cache_pagination_uses_message_time_when_old_history_was_backfilled_later(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    _record(store, "current", chat_id="group", thread_id="topic", text="current")
    _record(store, "old", chat_id="group", thread_id="topic", text="old")
    _record(store, "middle", chat_id="group", thread_id="topic", text="middle")
    with store.connect() as conn:
        conn.executemany(
            "UPDATE messages SET sent_at = ? WHERE message_id = ?",
            [
                ("2026-11-18T03:00:00Z", "current"),
                ("2026-11-18T01:00:00Z", "old"),
                ("2026-11-18T02:00:00Z", "middle"),
            ],
        )
    first = read_cached_history(store.path, app_id="app", chat_id="group", limit=2)
    assert [item["message_id"] for item in first["messages"]] == ["middle", "current"]
    second = read_cached_history(
        store.path,
        app_id="app",
        chat_id="group",
        limit=2,
        before_event_id=first["next_before_event_id"],
    )
    assert [item["message_id"] for item in second["messages"]] == ["old"]


def test_cache_preserves_reply_and_mention_details(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    event = {
        "sender": {"sender_id": {"open_id": "alice"}, "sender_type": "user"},
        "message": {
            "message_id": "m1",
            "chat_id": "group",
            "chat_type": "group",
            "thread_id": "topic",
            "parent_id": "parent",
            "message_type": "text",
            "content": json.dumps({"text": "@_user_1 请审核"}),
            "mentions": [
                {"key": "@_user_1", "name": "Bob", "id": {"open_id": "ou_bob"}}
            ],
        },
    }
    assert CoworkerStore(store).enqueue(
        app_id="app",
        bot_open_id="bot",
        raw={"header": {"event_id": "m1"}, "event": event},
    )
    message = MessageNormalizer(owner_open_id="bot").normalize(
        event_message_from_raw(event, bot_id="bot")
    )
    store.upsert_message_with_revision(message)
    result = read_cached_history(store.path, app_id="app", chat_id="group")
    cached = result["messages"][0]
    assert cached["reply_to_message_id"] == "parent"
    assert cached["mentions"] == [
        {"key": "@_user_1", "name": "Bob", "open_id": "ou_bob"}
    ]
    assert cached["mention_mapping_available"] is True

    # Old cached events retained IDs but lost the placeholder-to-person mapping.
    raw = event_message_from_raw(event, bot_id="bot")
    raw.pop("mention_details")
    with store.connect() as conn:
        conn.execute(
            "UPDATE messages SET raw_json = ? WHERE message_id = 'm1'",
            (json.dumps(raw),),
        )
    legacy = read_cached_history(store.path, app_id="app", chat_id="group")["messages"][
        0
    ]
    assert legacy["mentions"] == [{"open_id": "ou_bob"}]
    assert legacy["mention_mapping_available"] is False


def test_cache_time_filter_and_pagination_have_no_total_message_limit(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    for i in range(75):
        _record(store, f"m{i}", chat_id="group", thread_id="topic", text=f"item {i}")
    with store.connect() as conn:
        conn.execute("UPDATE messages SET sent_at = '2026-11-18T03:00:00Z'")
    cursor = None
    seen: set[str] = set()
    while True:
        page = read_cached_history(
            store.path,
            app_id="app",
            chat_id="group",
            since="2026-11-18T00:00:00Z",
            until="2026-11-19T00:00:00Z",
            before_event_id=cursor,
            limit=20,
        )
        seen.update(item["message_id"] for item in page["messages"])
        if not page["has_more"]:
            break
        cursor = page["next_before_event_id"]
    assert len(seen) == 75
    assert not read_cached_history(
        store.path, app_id="app", chat_id="group", until="2026-11-18T00:00:00Z"
    )["messages"]
