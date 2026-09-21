from __future__ import annotations

import sqlite3
from pathlib import Path

from feishu_shadow_agent.store.sqlite_store import SQLiteStore


class TracingStore(SQLiteStore):
    def __init__(self, path: Path) -> None:
        self.select_statements: list[str] = []
        super().__init__(path)

    def connect(self) -> sqlite3.Connection:
        conn = super().connect()
        conn.set_trace_callback(self._record_statement)
        return conn

    def _record_statement(self, statement: str) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.select_statements.append(statement)


def _seed_context_store(tmp_path: Path) -> tuple[TracingStore, int, int]:
    store = TracingStore(tmp_path / "agent.sqlite3")
    store.initialize()
    with store.connect() as conn:
        task_ids: list[int] = []
        for index in (1, 2):
            cursor = conn.execute(
                """
                INSERT INTO tasks(short_id, status, created_at, updated_at)
                VALUES (?, 'watching', ?, ?)
                """,
                (
                    f"t_context_{index}",
                    "2026-06-22T09:00:00+00:00",
                    "2026-06-22T09:00:00+00:00",
                ),
            )
            assert cursor.lastrowid is not None
            task_ids.append(int(cursor.lastrowid))

        messages = [
            ("m_root", "2026-06-22T10:00:00+00:00", "2026-06-22T10:00:00+00:00"),
            ("m_missing_time", None, "2026-06-22T10:01:00+00:00"),
            ("m_late", "2026-06-22T09:59:00+00:00", "2026-06-22T10:02:00+00:00"),
            ("m_new", "2026-06-22T10:03:00+00:00", "2026-06-22T10:03:00+00:00"),
            ("m_other_task", "2026-06-22T11:00:00+00:00", "2026-06-22T11:00:00+00:00"),
        ]
        for message_id, sent_at, inserted_at in messages:
            conn.execute(
                """
                INSERT INTO messages(
                  message_id, sent_at, text, normalized_json, raw_json, inserted_at
                ) VALUES (?, ?, ?, '{}', '{}', ?)
                """,
                (message_id, sent_at, message_id, inserted_at),
            )

        task_one_messages = [
            ("m_root", "root", "2026-06-22T10:00:00+00:00"),
            ("m_missing_time", "follow_up", "2026-06-22T10:01:00+00:00"),
            ("m_late", "follow_up", "2026-06-22T10:02:00+00:00"),
            ("m_new", "follow_up", "2026-06-22T10:03:00+00:00"),
        ]
        for message_id, role, created_at in task_one_messages:
            conn.execute(
                """
                INSERT INTO task_messages(task_id, message_id, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_ids[0], message_id, role, created_at),
            )
        conn.execute(
            """
            INSERT INTO task_messages(task_id, message_id, role, created_at)
            VALUES (?, 'm_other_task', 'root', ?)
            """,
            (task_ids[1], "2026-06-22T11:00:00+00:00"),
        )
    store.select_statements.clear()
    return store, task_ids[0], task_ids[1]


def test_recent_task_context_batches_queries_and_orders_by_message_time(
    tmp_path: Path,
) -> None:
    store, task_id, other_task_id = _seed_context_store(tmp_path)

    contexts = store.list_recent_task_context(
        [task_id, other_task_id, task_id], messages_per_task=3
    )

    assert list(contexts) == [task_id, other_task_id]
    assert contexts[task_id]["message_count"] == 4
    assert contexts[task_id]["truncated"] is True
    assert [
        message["message_id"] for message in contexts[task_id]["recent_messages"]
    ] == ["m_root", "m_missing_time", "m_new"]
    assert contexts[task_id]["recent_messages"][1]["sent_at"] is None
    assert contexts[other_task_id] == {
        "message_count": 1,
        "truncated": False,
        "recent_messages": [
            {
                "message_id": "m_other_task",
                "role": "root",
                "chat_id": None,
                "chat_type": None,
                "sender_id": None,
                "sender_name": None,
                "sender_role": "external_user_message",
                "sent_at": "2026-06-22T11:00:00+00:00",
                "thread_id": None,
                "reply_to_message_id": None,
                "text": "m_other_task",
            }
        ],
    }
    assert len(store.select_statements) == 2
    assert "COUNT(*)" in store.select_statements[0]
    assert "ROW_NUMBER()" in store.select_statements[1]


def test_recent_task_context_zero_limit_keeps_counts_without_messages(
    tmp_path: Path,
) -> None:
    store, task_id, other_task_id = _seed_context_store(tmp_path)

    contexts = store.list_recent_task_context(
        [task_id, other_task_id], messages_per_task=0
    )

    assert contexts[task_id] == {
        "message_count": 4,
        "truncated": True,
        "recent_messages": [],
    }
    assert contexts[other_task_id] == {
        "message_count": 1,
        "truncated": True,
        "recent_messages": [],
    }
    assert len(store.select_statements) == 1
    assert "COUNT(*)" in store.select_statements[0]
