from __future__ import annotations

import json
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
    "agent_audits",
    "approval_commands",
    "message_processing",
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
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, agent_session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_legacy", "watching", "oc_1", "om_1", "label", "feishu-task-t_legacy", "now", "now"),
        )
        task_id = conn.execute("SELECT id FROM tasks WHERE short_id = ?", ("t_legacy",)).fetchone()["id"]
    store.migrate()

    assert store.get_initialized_agent_session_id(task_id) is None
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


def test_agent_audits_include_tool_permissions_profile(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(agent_audits)").fetchall()
        }

    assert "tool_permissions_profile" in columns


def test_agent_backend_rename_migrates_existing_hermes_schema(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    with store.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              short_id TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              hermes_session_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE hermes_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              request_type TEXT NOT NULL,
              task_id INTEGER,
              hermes_session_id TEXT,
              input_message_ids_json TEXT NOT NULL DEFAULT '[]',
              input_resource_ids_json TEXT NOT NULL DEFAULT '[]',
              response_json TEXT,
              error TEXT,
              latency_ms INTEGER,
              prompt_json TEXT,
              created_at TEXT NOT NULL,
              tool_permissions_profile TEXT
            );
            CREATE INDEX idx_hermes_audits_task ON hermes_audits(task_id, created_at);
            INSERT INTO schema_migrations(version, applied_at) VALUES
              ('0001_foundation', 'now'),
              ('0002_ingestion_routing', 'now'),
              ('0003_hermes_approval', 'now'),
              ('0004_message_processing', 'now'),
              ('0005_hermes_tool_permissions', 'now');
            INSERT INTO tasks(short_id, status, hermes_session_id, created_at, updated_at) VALUES
              ('t_real', 'watching', 'sid_old', 'now', 'now'),
              ('t_fake', 'watching', 'feishu-task-t_fake', 'now', 'now');
            INSERT INTO hermes_audits(
              request_type, task_id, hermes_session_id, input_message_ids_json,
              input_resource_ids_json, created_at, tool_permissions_profile
            ) VALUES ('task_session', 1, 'sid_old', '[]', '[]', 'now', 'guarded_write');
            """
        )

    store.migrate()

    with store.connect() as conn:
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        audit_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(agent_audits)").fetchall()
        }
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        indexes = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        real_task = conn.execute("SELECT agent_session_id FROM tasks WHERE short_id = 't_real'").fetchone()
        fake_task = conn.execute("SELECT agent_session_id FROM tasks WHERE short_id = 't_fake'").fetchone()
        audit = conn.execute(
            "SELECT backend_provider, agent_session_id, tool_permissions_profile FROM agent_audits"
        ).fetchone()

    assert "agent_session_id" in task_columns
    assert "hermes_session_id" not in task_columns
    assert "agent_audits" in tables
    assert "hermes_audits" not in tables
    assert {"backend_provider", "agent_session_id", "tool_permissions_profile"} <= audit_columns
    assert "idx_agent_audits_task" in indexes
    assert "idx_hermes_audits_task" not in indexes
    assert real_task["agent_session_id"] == "sid_old"
    assert fake_task["agent_session_id"] is None
    assert dict(audit) == {
        "backend_provider": "hermes",
        "agent_session_id": "sid_old",
        "tool_permissions_profile": "guarded_write",
    }


def test_send_reply_retry_does_not_revive_failed_action_when_same_text_was_sent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("t_sent", "watching", "oc_1", "om_1", "label", "now", "now"),
        )
        task_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO actions(
              idempotency_key, task_id, kind, status, target_message_id, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "reply-failed-cross-source",
                task_id,
                "send_reply",
                "failed",
                "om_1",
                json.dumps({"text": "one", "source": "auto_reply"}),
                "now",
                "now",
            ),
        )
        conn.execute(
            """
            INSERT INTO actions(
              idempotency_key, task_id, kind, status, target_message_id, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "reply-sent-cross-source",
                task_id,
                "send_reply",
                "sent",
                "om_1",
                json.dumps({"text": "one", "source": "owner_send"}),
                "now",
                "now",
            ),
        )

    retried = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "one", "source": "approval_request"},
    )

    assert retried is None
    with store.connect() as conn:
        rows = conn.execute("SELECT status FROM actions ORDER BY id").fetchall()
    assert [row["status"] for row in rows] == ["failed", "sent"]


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
