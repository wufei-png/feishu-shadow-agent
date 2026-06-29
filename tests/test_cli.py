from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
owner:
  open_id: ou_owner
  name: Owner
storage:
  sqlite_path: agent.sqlite3
logging:
  jsonl_path: agent.jsonl
""".lstrip(),
        encoding="utf-8",
    )
    return config


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "agent.sqlite3")


def _insert_task(store: SQLiteStore, short_id: str, root_message_id: str) -> int:
    store.migrate()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(short_id, status, chat_id, root_message_id, task_label, created_at, updated_at, chat_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (short_id, "watching", "oc_1", root_message_id, "label", "now", "now", "p2p"),
        )
    return int(cursor.lastrowid)


def _insert_message(store: SQLiteStore, message_id: str) -> None:
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sent_at, text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                "oc_1",
                "p2p",
                "ou_a",
                "2026-06-22T10:00:00+08:00",
                "hello",
                json.dumps({"message_id": message_id}),
                "now",
            ),
        )


def test_send_preserves_multiword_text_and_local_command_id_is_unique(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_task(store, "t_1", "om_root")

    assert main(["send", "--config", str(config), "t_1", "hello", "world"]) == 0
    assert main(["reject", "--config", str(config), "a_missing"]) == 2
    assert main(["reject", "--config", str(config), "a_missing"]) == 2

    with store.connect() as conn:
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        commands = conn.execute("SELECT message_id, status FROM approval_commands ORDER BY id").fetchall()
    payload = json.loads(action["payload_json"])
    assert payload["text"] == "hello world"
    assert len(commands) == 3
    assert len({row["message_id"] for row in commands}) == 3
    assert commands[1]["status"] == "failed"
    assert commands[2]["status"] == "failed"


def test_send_can_read_exact_text_from_stdin(tmp_path: Path, monkeypatch) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_task(store, "t_2", "om_root_2")
    monkeypatch.setattr("sys.stdin", io.StringIO("line 1\n    line 2\n"))

    assert main(["send", "--config", str(config), "--stdin", "t_2"]) == 0

    with store.connect() as conn:
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
    payload = json.loads(action["payload_json"])
    assert payload["text"] == "line 1\n    line 2\n"


def test_daemon_send_owner_notifications_help_describes_dry_run_send(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["daemon", "--help"])

    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "run the daemon" in output
    assert "no-op daemon skeleton" not in output
    assert "do not send external replies" in output
    assert "record local state and dispatch previews" in output
    assert "actually send and consume owner_notification" in output
    assert "external replies stay pending" in output


def test_config_schema_outputs_json_schema(capsys) -> None:
    assert main(["config", "schema"]) == 0

    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tool_permissions"]["enum"] == ["read_only", "guarded_write", "full_access"]
    assert "description" in schema["properties"]["reply_policy"]
    assert (
        schema["$defs"]["ChatPolicyConfig"]["properties"]["reply_identity"]["description"]
        == "Reply identity for this chat: prefer bot with fallback, require bot, or send as user."
    )


def test_config_validate_returns_zero_for_valid_config(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)

    assert main(["config", "validate", "--config", str(config)]) == 0

    assert capsys.readouterr().out == f"config ok: {config}\n"


def test_config_validate_returns_two_for_invalid_config(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("tool_permissions: guarded_write\n", encoding="utf-8")

    assert main(["config", "validate", "--config", str(config)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config error:" in captured.err
    assert "owner" in captured.err


def test_status_includes_failed_approval_commands(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    assert main(["reject", "--config", str(config), "a_missing"]) == 2
    capsys.readouterr()

    assert main(["status", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["failed_approval_commands"][0]["status"] == "failed"
    assert output["failed_approval_commands"][0]["command"] == "/reject a_missing"


def test_status_active_tasks_excludes_expired_watch_windows(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_task(store, "t_expired", "om_expired")
    _insert_task(store, "t_active", "om_active")
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET watch_until = ? WHERE short_id = ?",
            ("2000-01-01T00:00:00+00:00", "t_expired"),
        )
        conn.execute(
            "UPDATE tasks SET watch_until = ? WHERE short_id = ?",
            ("2999-01-01T00:00:00+00:00", "t_active"),
        )

    assert main(["status", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    active_task_ids = {task["short_id"] for task in output["active_tasks"]}
    assert "t_active" in active_task_ids
    assert "t_expired" not in active_task_ids


def test_status_shows_pending_and_expires_overdue_approvals_in_real_db(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    pending_task_id = _insert_task(store, "t_pending", "om_pending")
    expired_task_id = _insert_task(store, "t_overdue", "om_overdue")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approvals(short_id, task_id, kind, status, payload_json, preview, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_pending",
                pending_task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_pending", "text": "pending reply"}),
                "pending reply",
                "2026-06-22T08:00:00+08:00",
                "2999-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO approvals(short_id, task_id, kind, status, payload_json, preview, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_overdue",
                expired_task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_overdue", "text": "overdue reply"}),
                "overdue reply",
                "2026-06-22T08:00:00+08:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )

    assert main(["status", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert {approval["short_id"] for approval in output["pending_approvals"]} == {"a_pending"}
    assert "a_overdue" in {approval["short_id"] for approval in output["recent_expired_approvals"]}
    with store.connect() as conn:
        rows = {
            row["short_id"]: row
            for row in conn.execute(
                "SELECT short_id, status, resolved_at FROM approvals ORDER BY short_id"
            ).fetchall()
        }
    assert rows["a_pending"]["status"] == "pending"
    assert rows["a_pending"]["resolved_at"] is None
    assert rows["a_overdue"]["status"] == "expired"
    assert rows["a_overdue"]["resolved_at"] is not None


def test_replay_explains_current_state_without_real_db_mutation(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_message(store, "om_1")

    assert main(["replay", "--config", str(config), "--message-id", "om_1", "--dry-run"]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["message_id"] == "om_1"
    assert output["mutated_real_db"] is False
    with store.connect() as conn:
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert action_count == 0


def test_replay_expires_pending_approvals_only_in_temp_db(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_message(store, "om_expired")
    task_id = _insert_task(store, "t_expired", "om_expired")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO task_messages(task_id, message_id, role, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "om_expired", "root", "2026-06-22T08:00:00+08:00"),
        )
        conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_expired",
                task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_expired", "text": "reply"}),
                "reply",
                "2026-06-22T08:00:00+08:00",
                "2026-06-22T09:00:00+08:00",
            ),
        )

    assert main(["replay", "--config", str(config), "--message-id", "om_expired", "--dry-run"]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["mutated_real_db"] is False
    assert output["state"]["approvals"][0]["status"] == "expired"
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_expired",),
        ).fetchone()
    assert approval["status"] == "pending"
    assert approval["resolved_at"] is None


def test_replay_previews_only_related_pending_actions(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    _insert_message(store, "om_1")
    related_task_id = _insert_task(store, "t_related", "om_1")
    unrelated_task_id = _insert_task(store, "t_unrelated", "om_other")
    related_action_id = store.create_send_reply_action(
        task_id=related_task_id,
        target_message_id="om_1",
        payload={"reply_target_message_id": "om_1", "text": "related", "identity": "user"},
    )
    unrelated_action_id = store.create_send_reply_action(
        task_id=unrelated_task_id,
        target_message_id="om_other",
        payload={"reply_target_message_id": "om_other", "text": "unrelated", "identity": "user"},
    )
    assert related_action_id is not None and unrelated_action_id is not None
    calls = []

    class FakeReplayClient:
        def __init__(self, **kwargs):
            pass

        def reply_message(self, **kwargs):
            calls.append(kwargs)
            return LarkCliResult(["dry"], 0, json_data={"api": [{"message_id": kwargs["message_id"]}]})

    monkeypatch.setattr("feishu_shadow_agent.cli.LarkCliClient", FakeReplayClient)

    assert main(["replay", "--config", str(config), "--message-id", "om_1", "--dry-run"]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    previews = output["dispatch_preview"]["actions"]
    assert output["dispatch_preview"]["processed"] == 1
    assert [preview["action_id"] for preview in previews] == [related_action_id]
    assert previews[0]["result"]["dry_run"]["json"]["api"][0]["message_id"] == "om_1"
    assert [call["message_id"] for call in calls] == ["om_1"]
    with store.connect() as conn:
        rows = conn.execute("SELECT id, result_json FROM actions ORDER BY id").fetchall()
    assert [(row["id"], row["result_json"]) for row in rows] == [
        (related_action_id, None),
        (unrelated_action_id, None),
    ]


def test_dispatch_inspect_is_read_only_and_shows_attempts(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_dispatch", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id, run_id="run_1") is not None

    assert main(["dispatch", "inspect", "--config", str(config), "--action-id", str(action_id)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["action"]["id"] == action_id
    assert output["action"]["status"] == "sending"
    assert output["attempts"][0]["status"] == "started"
    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "sending"


def test_dispatch_retry_requeues_failed_actions_and_preserves_idempotency(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_retry", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    original_key = store.get_action(action_id).idempotency_key  # type: ignore[union-attr]
    store.finish_action(action_id, status="failed_needs_review", result={"error_stage": "send"})

    assert main(["dispatch", "retry", "--config", str(config), "--action-id", str(action_id)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    action = store.get_action(action_id)
    assert output["status"] == "requeued"
    assert action is not None
    assert action.status == "pending"
    assert action.idempotency_key == original_key
    assert action.result == {}


def test_dispatch_retry_rejects_sending_actions(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_retry_sending", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id) is not None

    assert main(["dispatch", "retry", "--config", str(config), "--action-id", str(action_id)]) == 2

    assert "only accepts failed or failed_needs_review" in capsys.readouterr().err
    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "sending"


def test_dispatch_cancel_releases_active_send_target(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_cancel", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id) is not None

    assert main(["dispatch", "cancel", "--config", str(config), "--action-id", str(action_id)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "cancelled"
    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "cancelled"
    replacement = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "different", "identity": "user"},
    )
    assert replacement is not None
    assert replacement != action_id


def test_dispatch_mark_sent_requires_readback_evidence(tmp_path: Path, capsys, monkeypatch) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_mark_sent", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_1")
    assert claim is not None
    store.update_dispatch_attempt(claim.attempt.id, status="uncertain", error_stage="send")
    store.finish_action(action_id, status="failed_needs_review", result={"error_stage": "send"})

    class FakeMarkSentClient:
        def __init__(self, **kwargs):
            pass

        def get_messages(self, **kwargs):
            return MessagePage(
                [
                    {
                        "message_id": "om_sent",
                        "chat_id": "oc_1",
                        "chat_type": "p2p",
                        "sender_id": "ou_bot",
                        "sender_type": "bot",
                        "create_time": "2026-06-22T10:00:00+08:00",
                        "reply_to_message_id": "om_root",
                        "content": {"text": "reply"},
                    }
                ]
            )

    monkeypatch.setattr("feishu_shadow_agent.cli.LarkCliClient", FakeMarkSentClient)

    assert (
        main(
            [
                "dispatch",
                "mark-sent",
                "--config",
                str(config),
                "--action-id",
                str(action_id),
                "--sent-message-id",
                "om_sent",
            ]
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    action = store.get_action(action_id)
    attempts = store.list_dispatch_attempts(action_id)
    assert output["status"] == "sent"
    assert action is not None
    assert action.status == "sent"
    assert action.result["sent_message_id"] == "om_sent"
    assert action.result["readback"]["text"] == "reply"
    assert attempts[-1].status == "readback_ok"
    assert attempts[-1].error_stage is None
    assert attempts[-1].readback_result["ok"] is True


@pytest.mark.parametrize(
    ("payload", "message_overrides", "expected_error"),
    [
        (
            {"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
            {"reply_to_message_id": "om_other", "content": {"text": "reply"}},
            "reply_to_message_id",
        ),
        (
            {"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
            {"reply_to_message_id": "om_root", "content": {"text": "different"}},
            "text",
        ),
        (
            {
                "reply_target_message_id": "om_root",
                "text": '<at user_id="ou_expected">Alice</at> reply',
                "identity": "user",
            },
            {
                "reply_to_message_id": "om_root",
                "content": {"text": "reply", "mentions": [{"open_id": "ou_other"}]},
            },
            "mentions",
        ),
    ],
)
def test_dispatch_mark_sent_rejects_unverified_send_reply_evidence(
    tmp_path: Path,
    capsys,
    monkeypatch,
    payload: dict[str, object],
    message_overrides: dict[str, object],
    expected_error: str,
) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_mark_sent_strict", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload=payload,
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_1")
    assert claim is not None
    store.update_dispatch_attempt(claim.attempt.id, status="uncertain", error_stage="send")
    store.finish_action(action_id, status="failed_needs_review", result={"error_stage": "send"})

    class FakeMarkSentClient:
        def __init__(self, **kwargs):
            pass

        def get_messages(self, **kwargs):
            message = {
                "message_id": "om_sent",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "sender_id": "ou_bot",
                "sender_type": "bot",
                "create_time": "2026-06-22T10:00:00+08:00",
                **message_overrides,
            }
            return MessagePage([message])

    monkeypatch.setattr("feishu_shadow_agent.cli.LarkCliClient", FakeMarkSentClient)

    assert (
        main(
            [
                "dispatch",
                "mark-sent",
                "--config",
                str(config),
                "--action-id",
                str(action_id),
                "--sent-message-id",
                "om_sent",
            ]
        )
        == 2
    )

    output = yaml.safe_load(capsys.readouterr().out)
    action = store.get_action(action_id)
    attempts = store.list_dispatch_attempts(action_id)
    assert expected_error in output["error"]
    assert action is not None
    assert action.status == "failed_needs_review"
    assert attempts[-1].status == "uncertain"
    assert attempts[-1].error_stage == "send"
    with store.connect() as conn:
        message_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE message_id = ?",
            ("om_sent",),
        ).fetchone()["c"]
        task_message_count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_messages WHERE task_id = ? AND message_id = ?",
            (task_id, "om_sent"),
        ).fetchone()["c"]
    assert message_count == 0
    assert task_message_count == 0


def test_dispatch_mark_sent_cancel_race_does_not_persist_readback_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_mark_sent_cancelled", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_1")
    assert claim is not None
    store.update_dispatch_attempt(claim.attempt.id, status="uncertain", error_stage="send")
    store.finish_action(action_id, status="failed_needs_review", result={"error_stage": "send"})

    class FakeMarkSentClient:
        def __init__(self, **kwargs):
            pass

        def get_messages(self, **kwargs):
            return MessagePage(
                [
                    {
                        "message_id": "om_sent",
                        "chat_id": "oc_1",
                        "chat_type": "p2p",
                        "sender_id": "ou_bot",
                        "sender_type": "bot",
                        "create_time": "2026-06-22T10:00:00+08:00",
                        "reply_to_message_id": "om_root",
                        "content": {"text": "reply"},
                    }
                ]
            )

    original_mark_sent = SQLiteStore.mark_action_sent_after_evidence

    def cancel_before_mark_sent(self, action_id, **kwargs):
        self.cancel_dispatch_action(action_id)
        return original_mark_sent(self, action_id, **kwargs)

    monkeypatch.setattr("feishu_shadow_agent.cli.LarkCliClient", FakeMarkSentClient)
    monkeypatch.setattr(SQLiteStore, "mark_action_sent_after_evidence", cancel_before_mark_sent)

    assert (
        main(
            [
                "dispatch",
                "mark-sent",
                "--config",
                str(config),
                "--action-id",
                str(action_id),
                "--sent-message-id",
                "om_sent",
            ]
        )
        == 2
    )

    output = yaml.safe_load(capsys.readouterr().out)
    action = store.get_action(action_id)
    assert "cancelled actions cannot be marked sent" in output["error"]
    assert action is not None
    assert action.status == "cancelled"
    with store.connect() as conn:
        message_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE message_id = ?",
            ("om_sent",),
        ).fetchone()["c"]
        task_message_count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_messages WHERE task_id = ? AND message_id = ?",
            (task_id, "om_sent"),
        ).fetchone()["c"]
    assert message_count == 0
    assert task_message_count == 0


def test_dispatch_mark_sent_requires_sent_message_id() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["dispatch", "mark-sent", "--action-id", "1"])

    assert exc.value.code == 2
