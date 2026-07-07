from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult, StateSchemaContract

EXPECTED_TABLES = {
    "schema_migrations",
    "messages",
    "tasks",
    "task_messages",
    "task_watch_keys",
    "approvals",
    "actions",
    "dispatch_attempts",
    "resources",
    "checkpoints",
    "runs",
    "health_checks",
    "chat_policies",
    "product_policies",
    "policy_audits",
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
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
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


def test_sqlite_connections_apply_busy_timeout(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    with store.connect() as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout == 5000


def test_checkpoint_and_run_health_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    store.set_checkpoint(
        "ingest.group_at_me", {"last_success_at": "2026-06-22T00:00:00+08:00"}
    )
    store.record_run_start(
        run_id="run_1", dry_run=True, git_commit="abc123", git_dirty=False
    )
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
    store.record_run_finish(
        run_id="run_1", status="ok", health_summary={"critical_failed": []}
    )

    assert store.get_checkpoint("ingest.group_at_me") == {
        "last_success_at": "2026-06-22T00:00:00+08:00"
    }
    with store.connect() as conn:
        run = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?", ("run_1",)
        ).fetchone()
        health = conn.execute(
            "SELECT check_name FROM health_checks WHERE run_id = ?", ("run_1",)
        ).fetchone()
    assert run["status"] == "ok"
    assert health["check_name"] == "config_schema"


def test_baseline_schema_includes_current_columns(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        message_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        approval_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(approvals)").fetchall()
        }
        audit_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(agent_audits)").fetchall()
        }
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        attempt_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(dispatch_attempts)").fetchall()
        }
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        product_policy_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(product_policies)").fetchall()
        }
        policy_audit_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(policy_audits)").fetchall()
        }

    assert {
        "thread_id",
        "reply_to_message_id",
        "sender_role",
        "direct_mention",
        "at_all",
        "text",
    } <= message_columns
    assert {
        "chat_type",
        "thread_id",
        "watch_until",
        "last_user_message",
        "last_agent_reply",
        "agent_session_id",
        "agent_session_provider",
        "agent_working_dir",
    } <= task_columns
    assert "hermes_session_id" not in task_columns
    assert "expires_at" in approval_columns
    assert "sender_name" in message_columns
    assert {
        "backend_provider",
        "agent_session_id",
        "tool_permissions_profile",
    } <= audit_columns
    assert {
        "action_id",
        "run_id",
        "claim_token",
        "status",
        "dry_run_result_json",
        "send_result_json",
        "readback_result_json",
        "sent_message_id",
        "error_stage",
        "started_at",
        "finished_at",
    } <= attempt_columns
    assert {
        "last_heartbeat_at",
        "last_tick_started_at",
        "last_tick_finished_at",
        "last_tick_status",
        "last_tick_summary_json",
    } <= run_columns
    assert {"key", "policy_json", "updated_at"} <= product_policy_columns
    assert {
        "scope",
        "policy_key",
        "actor",
        "old_json",
        "new_json",
        "reason",
        "created_at",
    } <= policy_audit_columns
    assert "idx_agent_audits_task" in indexes
    assert "idx_actions_active_send_reply_target" in indexes
    assert "idx_dispatch_attempts_action" in indexes
    assert "idx_policy_audits_policy" in indexes


def test_migration_relaxes_legacy_policy_audit_new_json_for_delete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES
              ('0001_foundation', 'now'),
              ('0002_add_task_agent_working_dir', 'now');

            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              short_id TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              agent_session_id TEXT,
              agent_working_dir TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE chat_policies (
              chat_id TEXT PRIMARY KEY,
              name TEXT,
              auto_reply INTEGER NOT NULL DEFAULT 0,
              bot_joined INTEGER NOT NULL DEFAULT 0,
              reply_identity TEXT NOT NULL DEFAULT 'bot_preferred',
              allow_user_fallback INTEGER NOT NULL DEFAULT 1,
              resource_download INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            INSERT INTO chat_policies(
              chat_id, name, auto_reply, bot_joined, reply_identity,
              allow_user_fallback, resource_download, updated_at
            )
            VALUES ('oc_legacy', 'Legacy', 1, 0, 'bot_preferred', 1, 1, 'now');

            CREATE TABLE policy_audits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scope TEXT NOT NULL CHECK (scope IN ('global', 'chat')),
              policy_key TEXT NOT NULL,
              actor TEXT NOT NULL,
              old_json TEXT,
              new_json TEXT NOT NULL,
              reason TEXT,
              created_at TEXT NOT NULL
            );
            INSERT INTO policy_audits(
              scope, policy_key, actor, old_json, new_json, reason, created_at
            )
            VALUES ('chat', 'chat:oc_legacy', 'test', NULL, '{}', 'seed', 'now');
            CREATE INDEX idx_policy_audits_policy
            ON policy_audits(policy_key, created_at);
            """
        )

    store = SQLiteStore(db_path)

    result = store.delete_chat_product_policy(
        "oc_legacy",
        actor="test_operator",
        reason="remove legacy override",
    )

    assert result["changed"] is True
    with store.connect() as conn:
        policy_audit_columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(policy_audits)").fetchall()
        }
        audit = conn.execute(
            """
            SELECT new_json
            FROM policy_audits
            WHERE policy_key = 'chat:oc_legacy'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        migration = conn.execute(
            """
            SELECT 1
            FROM schema_migrations
            WHERE version = '0003_relax_policy_audit_delete_json'
            """
        ).fetchone()

    assert policy_audit_columns["new_json"]["notnull"] == 0
    assert audit["new_json"] is None
    assert migration is not None


def test_migration_backfills_existing_task_session_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES
              ('0001_foundation', 'now'),
              ('0002_add_task_agent_working_dir', 'now'),
              ('0003_relax_policy_audit_delete_json', 'now');

            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              short_id TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              agent_session_id TEXT,
              agent_working_dir TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO tasks(
              short_id, status, agent_session_id, created_at, updated_at
            )
            VALUES
              ('t_with_session', 'watching', 'sid_1', 'now', 'now'),
              ('t_without_session', 'watching', NULL, 'now', 'now');
            """
        )

    store = SQLiteStore(db_path)
    store.migrate()

    with store.connect() as conn:
        rows = {
            row["short_id"]: row["agent_session_provider"]
            for row in conn.execute(
                """
                SELECT short_id, agent_session_provider
                FROM tasks
                ORDER BY short_id
                """
            ).fetchall()
        }

    assert rows == {"t_with_session": "hermes", "t_without_session": None}


def test_send_reply_guard_and_failed_retry_use_current_baseline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, agent_session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t_current",
                "watching",
                "oc_1",
                "om_1",
                "label",
                "sid_current",
                "now",
                "now",
            ),
        )
        task_id = conn.execute(
            "SELECT id FROM tasks WHERE short_id = ?", ("t_current",)
        ).fetchone()["id"]
    store.migrate()

    store.set_task_agent_session_id(task_id, "sid_current", backend_provider="hermes")
    assert (
        store.get_initialized_agent_session_id(task_id, backend_provider="hermes")
        == "sid_current"
    )
    assert (
        store.get_initialized_agent_session_id(task_id, backend_provider="codex")
        is None
    )
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
    owner_action = store.create_owner_notification_action(
        task_id=task_id, payload={"text": "notify"}
    )

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


def test_claim_creates_dispatch_attempt_and_retry_preserves_idempotency_key(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("t_dispatch", "watching", "oc_1", "om_1", "now", "now"),
        )
        task_id = int(
            conn.execute(
                "SELECT id FROM tasks WHERE short_id = ?", ("t_dispatch",)
            ).fetchone()["id"]
        )

    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "one", "source": "auto_reply"},
    )
    assert action_id is not None
    original_key = store.get_action(action_id).idempotency_key  # type: ignore[union-attr]

    claim = store.claim_action_for_dispatch(action_id, run_id="run_1")

    assert claim is not None
    assert claim.action.status == "sending"
    assert claim.attempt.action_id == action_id
    assert claim.attempt.status == "started"
    assert claim.attempt.run_id == "run_1"
    assert claim.attempt.claim_token

    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )
    retried = store.retry_dispatch_action(action_id)

    assert retried.status == "pending"
    assert retried.idempotency_key == original_key
    assert retried.result == {}


def test_claim_aware_finish_does_not_overwrite_operator_cancel(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("t_cancel_claim", "watching", "oc_1", "om_1", "now", "now"),
        )
        task_id = int(
            conn.execute(
                "SELECT id FROM tasks WHERE short_id = ?", ("t_cancel_claim",)
            ).fetchone()["id"]
        )

    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_1",
        payload={"text": "one", "source": "auto_reply"},
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_1")
    assert claim is not None

    cancelled = store.cancel_dispatch_action(action_id)
    finished = store.finish_claimed_action(
        action_id,
        attempt_id=claim.attempt.id,
        status="sent",
        result={"sent_message_id": "om_sent"},
    )

    assert cancelled.status == "cancelled"
    assert finished is None
    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "cancelled"
    assert action.result == {}


def test_state_schema_contract_accepts_all_enum_values(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        for index, status in enumerate(StateSchemaContract.task_statuses):
            conn.execute(
                "INSERT INTO tasks(short_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (f"t_status_{index}", status, "now", "now"),
            )
        for index, chat_type in enumerate(StateSchemaContract.chat_types):
            conn.execute(
                "INSERT INTO messages(message_id, chat_type, raw_json, inserted_at) VALUES (?, ?, ?, ?)",
                (f"om_chat_{index}", chat_type, "{}", "now"),
            )
        for index, sender_role in enumerate(StateSchemaContract.sender_roles):
            conn.execute(
                "INSERT INTO messages(message_id, sender_role, raw_json, inserted_at) VALUES (?, ?, ?, ?)",
                (f"om_sender_{index}", sender_role, "{}", "now"),
            )
        for index, kind in enumerate(StateSchemaContract.approval_kinds):
            conn.execute(
                "INSERT INTO approvals(short_id, kind, status, created_at) VALUES (?, ?, ?, ?)",
                (f"a_kind_{index}", kind, "pending", "now"),
            )
        for index, status in enumerate(StateSchemaContract.approval_statuses):
            conn.execute(
                "INSERT INTO approvals(short_id, kind, status, created_at) VALUES (?, ?, ?, ?)",
                (f"a_status_{index}", "send_reply", status, "now"),
            )
        for index, kind in enumerate(StateSchemaContract.action_kinds):
            conn.execute(
                """
                INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"action-kind-{index}", kind, "pending", "now", "now"),
            )
        for index, status in enumerate(StateSchemaContract.action_statuses):
            conn.execute(
                """
                INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"action-status-{index}", "send_reply", status, "now", "now"),
            )
        conn.execute(
            """
            INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("action-for-attempts", "send_reply", "pending", "now", "now"),
        )
        action_id = int(
            conn.execute(
                "SELECT id FROM actions WHERE idempotency_key = ?",
                ("action-for-attempts",),
            ).fetchone()["id"]
        )
        for index, status in enumerate(StateSchemaContract.dispatch_attempt_statuses):
            conn.execute(
                """
                INSERT INTO dispatch_attempts(action_id, claim_token, status, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (action_id, f"claim-status-{index}", status, "now"),
            )
        for index, error_stage in enumerate(StateSchemaContract.dispatch_error_stages):
            conn.execute(
                """
                INSERT INTO dispatch_attempts(action_id, claim_token, status, error_stage, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (action_id, f"claim-stage-{index}", "failed", error_stage, "now"),
            )
        for index, status in enumerate(StateSchemaContract.resource_statuses):
            conn.execute(
                """
                INSERT INTO resources(message_id, file_key, resource_type, download_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"om_res_{index}", f"file_{index}", "file", status, "now", "now"),
            )
        for index, status in enumerate(StateSchemaContract.run_tick_statuses):
            conn.execute(
                """
                INSERT INTO runs(run_id, started_at, status, dry_run, last_tick_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"run_tick_{index}", "now", "running", 1, status),
            )
        for index, route in enumerate(StateSchemaContract.route_names):
            conn.execute(
                "INSERT INTO routing_audits(message_id, route, created_at) VALUES (?, ?, ?)",
                (f"om_route_{index}", route, "now"),
            )
        for index, stage in enumerate(StateSchemaContract.message_processing_stages):
            conn.execute(
                """
                INSERT INTO message_processing(message_id, stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"om_stage_{index}", stage, "processed", "now", "now"),
            )
        for index, status in enumerate(StateSchemaContract.message_processing_statuses):
            conn.execute(
                """
                INSERT INTO message_processing(message_id, stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"om_mp_status_{index}", "task_router", status, "now", "now"),
            )


@pytest.mark.parametrize(
    "sql, params",
    [
        (
            "INSERT INTO tasks(short_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("t_bad", "waiting_approval", "now", "now"),
        ),
        (
            "INSERT INTO approvals(short_id, kind, status, created_at) VALUES (?, ?, ?, ?)",
            ("a_bad_kind", "future_kind", "pending", "now"),
        ),
        (
            "INSERT INTO approvals(short_id, kind, status, created_at) VALUES (?, ?, ?, ?)",
            ("a_bad_status", "send_reply", "future_status", "now"),
        ),
        (
            "INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("action-bad-kind", "future_kind", "pending", "now", "now"),
        ),
        (
            "INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("action-bad-status", "send_reply", "future_status", "now", "now"),
        ),
        (
            """
            INSERT INTO resources(message_id, file_key, resource_type, download_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("om_bad_resource", "file_bad", "file", "future_status", "now", "now"),
        ),
        (
            "INSERT INTO routing_audits(message_id, route, created_at) VALUES (?, ?, ?)",
            ("om_bad_route", "future_route", "now"),
        ),
        (
            """
            INSERT INTO message_processing(message_id, stage, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("om_bad_stage", "future_stage", "processed", "now", "now"),
        ),
        (
            """
            INSERT INTO message_processing(message_id, stage, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("om_bad_mp_status", "task_router", "future_status", "now", "now"),
        ),
        (
            "INSERT INTO messages(message_id, chat_type, raw_json, inserted_at) VALUES (?, ?, ?, ?)",
            ("om_bad_chat", "future_chat", "{}", "now"),
        ),
        (
            "INSERT INTO messages(message_id, sender_role, raw_json, inserted_at) VALUES (?, ?, ?, ?)",
            ("om_bad_sender", "future_sender", "{}", "now"),
        ),
        (
            "INSERT INTO runs(run_id, started_at, status, dry_run, last_tick_status) VALUES (?, ?, ?, ?, ?)",
            ("run_bad_tick", "now", "running", 1, "future_tick"),
        ),
    ],
)
def test_invalid_state_values_fail_db_check(
    tmp_path: Path, sql: str, params: tuple[object, ...]
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


@pytest.mark.parametrize(
    "column, value",
    [
        ("status", "future_status"),
        ("error_stage", "future_stage"),
    ],
)
def test_invalid_dispatch_attempt_values_fail_db_check(
    tmp_path: Path, column: str, value: str
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    store.migrate()

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO actions(idempotency_key, kind, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("action-for-bad-attempt", "send_reply", "pending", "now", "now"),
        )
        action_id = int(conn.execute("SELECT id FROM actions").fetchone()["id"])
        sql = """
            INSERT INTO dispatch_attempts(action_id, claim_token, status, error_stage, started_at)
            VALUES (?, ?, ?, ?, ?)
            """
        params = (
            action_id,
            f"claim-bad-{column}",
            value if column == "status" else "failed",
            value if column == "error_stage" else None,
            "now",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, params)


def test_send_reply_retry_does_not_revive_failed_action_when_same_text_was_sent(
    tmp_path: Path,
) -> None:
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

    active_action = store.create_owner_notification_action(
        task_id=None, payload=payload
    )
    duplicate_pending = store.create_owner_notification_action(
        task_id=None, payload=payload
    )
    assert duplicate_pending == active_action
    assert store.claim_action_for_dispatch(active_action) is not None

    duplicate_sending = store.create_owner_notification_action(
        task_id=None, payload=payload
    )
    assert duplicate_sending == active_action
    sending_action = store.get_action(active_action)
    assert sending_action is not None
    assert sending_action.status == "sending"

    store.finish_action(
        active_action, status="sent", result={"sent_message_id": "om_owner"}
    )
    duplicate_sent = store.create_owner_notification_action(
        task_id=None, payload=payload
    )
    assert duplicate_sent == active_action
    sent_action = store.get_action(active_action)
    assert sent_action is not None
    assert sent_action.status == "sent"
    assert sent_action.result == {"sent_message_id": "om_owner"}

    retry_payload = {"type": "notify", "message": "retry"}
    failed_action = store.create_owner_notification_action(
        task_id=None, payload=retry_payload
    )
    store.finish_action(failed_action, status="failed", result={"error_stage": "send"})

    retried = store.create_owner_notification_action(
        task_id=None, payload=retry_payload
    )

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
