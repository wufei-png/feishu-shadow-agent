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
