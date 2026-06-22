from __future__ import annotations

import sqlite3
from pathlib import Path

from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


EXPECTED_TABLES = {
    "schema_migrations",
    "messages",
    "tasks",
    "task_messages",
    "task_watch_keys",
    "approvals",
    "actions",
    "resources",
    "checkpoints",
    "runs",
    "health_checks",
    "chat_policies",
    "config_suggestions",
    "routing_audits",
    "hermes_audits",
    "approval_commands",
}


def test_migration_is_idempotent_and_creates_tables(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    store.migrate()
    store.migrate()

    with store.connect() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert EXPECTED_TABLES <= {row["name"] for row in rows}


def test_unique_constraints_are_enforced(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        conn.execute(
            "INSERT INTO messages(message_id, raw_json, inserted_at) VALUES (?, ?, ?)",
            ("om_1", "{}", "now"),
        )
        try:
            conn.execute(
                "INSERT INTO messages(message_id, raw_json, inserted_at) VALUES (?, ?, ?)",
                ("om_1", "{}", "now"),
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("messages.message_id unique constraint did not fire")


def test_checkpoint_and_run_health_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    store.set_checkpoint("ingest.group_at_me", {"last_success_at": "2026-06-22T00:00:00+08:00"})
    store.record_run_start(run_id="run_1", dry_run=True, git_commit="abc123", git_dirty=False)
    store.record_health_results(
        run_id="run_1",
        results=[
            HealthCheckResult(
                "config_schema",
                "critical",
                "ok",
                "ok",
                {"path": "config.yaml"},
            )
        ],
    )
    store.record_run_finish(run_id="run_1", status="ok", health_summary={"critical_failed": []})

    assert store.get_checkpoint("ingest.group_at_me") == {
        "last_success_at": "2026-06-22T00:00:00+08:00"
    }
    with store.connect() as conn:
        run = conn.execute("SELECT status FROM runs WHERE run_id = ?", ("run_1",)).fetchone()
        health = conn.execute("SELECT check_name FROM health_checks WHERE run_id = ?", ("run_1",)).fetchone()
    assert run["status"] == "ok"
    assert health["check_name"] == "config_schema"


def test_p2_migration_adds_routing_columns(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        message_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }

    assert {"thread_id", "reply_to_message_id", "sender_role", "direct_mention", "at_all", "text"} <= message_columns
    assert {"chat_type", "thread_id", "watch_until", "last_user_message", "last_agent_reply"} <= task_columns
    assert "sender_name" in message_columns


def test_p3_migration_clears_legacy_fake_hermes_session_and_send_reply_guard(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, hermes_session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_legacy", "watching", "oc_1", "om_1", "label", "feishu-task-t_legacy", "now", "now"),
        )
        task_id = conn.execute("SELECT id FROM tasks WHERE short_id = ?", ("t_legacy",)).fetchone()["id"]
    store.migrate()

    assert store.get_initialized_hermes_session_id(task_id) is None
    first = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "one", "source": "auto_reply"},
    )
    second = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "two", "source": "auto_reply"},
    )
    owner_action = store.create_owner_notification_action(task_id=task_id, payload={"text": "notify"})

    assert first is not None
    assert second is None
    assert owner_action is not None

    store.finish_action(first, status="failed", result={"error_stage": "send"})
    retried = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "one", "source": "auto_reply"},
    )

    assert retried == first
    retried_action = store.get_action(first)
    assert retried_action is not None
    assert retried_action.status == "pending"
    assert retried_action.result == {}


def test_owner_notification_failed_action_is_reused_but_active_and_sent_are_not_duplicated(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    payload = {"type": "notify", "message": "same"}

    active_action = store.create_owner_notification_action(task_id=None, payload=payload)
    duplicate_pending = store.create_owner_notification_action(task_id=None, payload=payload)
    assert duplicate_pending == active_action
    assert store.claim_action_for_dispatch(active_action) is not None

    duplicate_sending = store.create_owner_notification_action(task_id=None, payload=payload)
    assert duplicate_sending == active_action
    sending_action = store.get_action(active_action)
    assert sending_action is not None
    assert sending_action.status == "sending"

    store.finish_action(active_action, status="sent", result={"sent_message_id": "om_owner"})
    duplicate_sent = store.create_owner_notification_action(task_id=None, payload=payload)
    assert duplicate_sent == active_action
    sent_action = store.get_action(active_action)
    assert sent_action is not None
    assert sent_action.status == "sent"
    assert sent_action.result == {"sent_message_id": "om_owner"}

    retry_payload = {"type": "notify", "message": "retry"}
    failed_action = store.create_owner_notification_action(task_id=None, payload=retry_payload)
    store.finish_action(failed_action, status="failed", result={"error_stage": "send"})

    retried = store.create_owner_notification_action(task_id=None, payload=retry_payload)

    assert retried == failed_action
    retried_action = store.get_action(failed_action)
    assert retried_action is not None
    assert retried_action.status == "pending"
    assert retried_action.result == {}
    with store.connect() as conn:
        owner_actions = conn.execute(
            "SELECT id, status FROM actions WHERE kind = 'owner_notification' ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["status"]) for row in owner_actions] == [
        (active_action, "sent"),
        (failed_action, "pending"),
    ]
