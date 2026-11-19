from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.retention import RetentionService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def test_retention_protects_pending_source_and_expires_finished_answers(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    store.initialize()
    with store.connect() as conn:
        for message in ("pending", "sent"):
            conn.execute(
                """INSERT INTO messages(message_id, chat_id, text, raw_json, inserted_at)
                   VALUES (?, 'group', 'source', '{}', '2020-01-01T00:00:00Z')""",
                (message,),
            )
            session = conn.execute(
                """INSERT INTO coworker_sessions(app_id, chat_id, thread_id, provider,
                   agent_working_dir, created_at, updated_at) VALUES ('app', 'group', ?,
                   'codex', '/tmp', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')
                   RETURNING id""",
                (message,),
            ).fetchone()[0]
            event = conn.execute(
                """INSERT INTO coworker_events(app_id, message_id, chat_id, topic_key,
                   raw_json, status, received_at, updated_at)
                   VALUES ('app', ?, 'group', ?, '{}', 'processing',
                           '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z') RETURNING id""",
                (message, message),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO coworker_native_deliveries(event_id, session_id,
                   source_revision, source_message_id, target_message_id, text, idempotency_key, status,
                   created_at, updated_at) VALUES (?, ?, 1, ?, ?, 'private answer', ?, ?,
                   '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')""",
                (
                    event,
                    session,
                    message,
                    message,
                    message,
                    "sent" if message == "sent" else "pending",
                ),
            )
            conn.execute(
                "UPDATE coworker_events SET status = 'completed' WHERE id = ?", (event,)
            )
    retention = RetentionService(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="owner")),
        base_dir=tmp_path,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    preview = retention.prune(now=now, dry_run=True, protect_pending=True)
    assert preview.content_candidates["coworker_native_deliveries"] == 1
    retention.prune(now=now, protect_pending=True)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT target_message_id, text FROM coworker_native_deliveries ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("pending", "private answer"),
            ("sent", ""),
        ]
        assert (
            conn.execute(
                "SELECT text FROM messages WHERE message_id='pending'"
            ).fetchone()[0]
            == "source"
        )
