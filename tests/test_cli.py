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


def test_approve_and_reject_emit_operator_command_result(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    approve_task_id = _insert_task(store, "t_approve", "om_approve_root")
    reject_task_id = _insert_task(store, "t_reject", "om_reject_root")
    approve_id = store.create_send_reply_approval(
        task_id=approve_task_id,
        preview="approved reply",
        payload={
            "reply_target_message_id": "om_approve_root",
            "text": "approved reply",
            "identity": "user",
            "source": "approval_request",
        },
        approval_timeout_hours=None,
    )
    reject_id = store.create_send_reply_approval(
        task_id=reject_task_id,
        preview="rejected reply",
        payload={
            "reply_target_message_id": "om_reject_root",
            "text": "rejected reply",
            "identity": "user",
            "source": "approval_request",
        },
        approval_timeout_hours=None,
    )
    with store.connect() as conn:
        approve_short_id = conn.execute("SELECT short_id FROM approvals WHERE id = ?", (approve_id,)).fetchone()["short_id"]
        reject_short_id = conn.execute("SELECT short_id FROM approvals WHERE id = ?", (reject_id,)).fetchone()["short_id"]

    assert main(["approve", "--config", str(config), approve_short_id]) == 0
    approve_output = yaml.safe_load(capsys.readouterr().out)
    assert approve_output["status"] == "applied"
    assert approve_output["command"] == "approval.approve"
    assert approve_output["actor"] == "local_cli"
    assert approve_output["target"] == {"type": "approval_or_task", "id": approve_short_id}
    assert approve_output["changed"] is True
    assert approve_output["result"]["approval_command_status"] == "applied"
    assert approve_output["next_actions"][0]["command"] == "dispatch.inspect"

    assert main(["reject", "--config", str(config), reject_short_id]) == 0
    reject_output = yaml.safe_load(capsys.readouterr().out)
    assert reject_output["status"] == "applied"
    assert reject_output["command"] == "approval.reject"
    assert reject_output["actor"] == "local_cli"
    assert reject_output["target"] == {"type": "approval_or_task", "id": reject_short_id}
    assert reject_output["changed"] is True
    assert reject_output["result"]["approval_command_status"] == "applied"
    assert reject_output["next_actions"] == []

    with store.connect() as conn:
        approvals = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM approvals WHERE id IN (?, ?)", (approve_id, reject_id))
        }
        approved_actions = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply' AND approval_id = ?",
            (approve_id,),
        ).fetchone()["c"]
    assert approvals == {approve_id: "approved", reject_id: "rejected"}
    assert approved_actions == 1


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
    assert output["policy_status"]["initialized"] is False
    assert output["policy_status"]["policy_import_diff"]["status"] == "differs"
    assert "policy_audits" not in output


def test_status_does_not_create_missing_store_or_log_files(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
owner:
  open_id: ou_owner
  name: Owner
storage:
  sqlite_path: data/missing.sqlite3
logging:
  jsonl_path: logs/agent.jsonl
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["status", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["daemon_liveness"]["status"] == "not_started"
    assert output["policy_status"]["initialized"] is False
    assert output["policy_status"]["policy_import_diff"]["status"] == "differs"
    assert not (tmp_path / "data" / "missing.sqlite3").exists()
    assert not (tmp_path / "logs" / "agent.jsonl").exists()


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


def test_status_shows_overdue_pending_approvals_without_expiring_real_db(tmp_path: Path, capsys) -> None:
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
    approvals = {approval["short_id"]: approval for approval in output["pending_approvals"]}
    assert set(approvals) == {"a_pending", "a_overdue"}
    assert approvals["a_pending"]["status"] == "pending"
    assert approvals["a_pending"]["is_overdue"] is False
    assert approvals["a_pending"]["overdue_seconds"] == 0
    assert approvals["a_pending"]["recommended_action"] == "review"
    assert approvals["a_overdue"]["status"] == "pending"
    assert approvals["a_overdue"]["is_overdue"] is True
    assert approvals["a_overdue"]["overdue_seconds"] > 0
    assert approvals["a_overdue"]["recommended_action"] == "expire"
    assert output["recent_expired_approvals"] == []
    with store.connect() as conn:
        rows = {
            row["short_id"]: row
            for row in conn.execute(
                "SELECT short_id, status, resolved_at FROM approvals ORDER BY short_id"
            ).fetchall()
        }
    assert rows["a_pending"]["status"] == "pending"
    assert rows["a_pending"]["resolved_at"] is None
    assert rows["a_overdue"]["status"] == "pending"
    assert rows["a_overdue"]["resolved_at"] is None


def test_policy_import_config_uses_defaults_when_reply_policy_is_omitted(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)

    assert main(["policy", "import-config", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["command"] == "policy.import_config"
    assert output["actor"] == "local_cli"
    assert output["changed"] is True
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert output["policy_import_diff"]["status"] == "matches"
    assert output["result"]["used_defaults"] is True
    assert output["result"]["inserted"]["global"] == ["reply_policy"]
    assert output["result"]["audit_count"] == 1
    assert output["result"]["initialization"] == {"initialized": True, "missing": []}
    product_policy = store.get_product_policy()
    assert product_policy is not None
    assert product_policy["reply_policy"] == {
        "p2p_auto_reply": True,
        "unknown_group_auto_reply": False,
    }


def test_policy_import_config_replace_updates_config_listed_chats(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        + """
chats:
  oc_replace:
    name: Before
    auto_reply: true
  oc_absent:
    name: Not in replacement
    auto_reply: true
""",
        encoding="utf-8",
    )
    assert main(["policy", "import-config", "--config", str(config)]) == 0
    capsys.readouterr()

    config.write_text(
        """
owner:
  open_id: ou_owner
  name: Owner
storage:
  sqlite_path: agent.sqlite3
logging:
  jsonl_path: agent.jsonl
reply_policy:
  p2p_auto_reply: false
  unknown_group_auto_reply: true
chats:
  oc_replace:
    name: After
    auto_reply: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["policy", "import-config", "--config", str(config), "--replace"]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["command"] == "policy.import_config"
    assert output["result"]["used_defaults"] is False
    assert output["result"]["replaced"]["global"] == ["reply_policy"]
    assert output["result"]["replaced"]["chats"] == ["oc_replace"]
    assert output["policy_import_diff"]["status"] == "matches"
    store = _store(tmp_path)
    product_policy = store.get_product_policy()
    assert product_policy is not None
    assert product_policy["reply_policy"] == {
        "p2p_auto_reply": False,
        "unknown_group_auto_reply": True,
    }
    replaced_chat = store.get_chat_product_policy("oc_replace")
    absent_chat = store.get_chat_product_policy("oc_absent")
    assert replaced_chat is not None
    assert absent_chat is not None
    assert replaced_chat["name"] == "After"
    assert absent_chat["name"] == "Not in replacement"


def test_policy_update_global_expansion_applies_directly(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        + """
reply_policy:
  p2p_auto_reply: false
  unknown_group_auto_reply: false
""",
        encoding="utf-8",
    )
    store = _store(tmp_path)
    assert main(["policy", "import-config", "--config", str(config)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "policy",
                "update-global",
                "--config",
                str(config),
                "--p2p-auto-reply",
                "true",
                "--reason",
                "enable p2p trial",
            ]
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["command"] == "policy.update_global"
    assert output["actor"] == "local_cli"
    assert output["changed"] is True
    assert output["warnings"] == []
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert store.get_product_policy()["reply_policy"]["p2p_auto_reply"] is True
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "local_cli"
    assert audit["reason"] == "enable p2p trial"


def test_policy_update_chat_narrowing_change_writes_audit(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        + """
chats:
  oc_policy:
    name: Policy group
    auto_reply: true
    resource_download: true
""",
        encoding="utf-8",
    )
    store = _store(tmp_path)
    assert main(["policy", "import-config", "--config", str(config)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "policy",
                "update-chat",
                "--config",
                str(config),
                "--chat-id",
                "oc_policy",
                "--auto-reply",
                "false",
                "--reason",
                "pause chat",
            ]
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["command"] == "policy.update_chat"
    assert output["warnings"] == []
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert store.get_chat_product_policy("oc_policy")["auto_reply"] is False
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "local_cli"
    assert audit["reason"] == "pause chat"


def test_policy_update_chat_bot_joined_expansion_applies_directly(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8")
        + """
chats:
  oc_policy:
    name: Policy group
    auto_reply: true
    bot_joined: false
    reply_identity: bot_preferred
    resource_download: true
""",
        encoding="utf-8",
    )
    store = _store(tmp_path)
    assert main(["policy", "import-config", "--config", str(config)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "policy",
                "update-chat",
                "--config",
                str(config),
                "--chat-id",
                "oc_policy",
                "--bot-joined",
                "true",
            ]
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["changed"] is True
    assert output["warnings"] == []
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert store.get_chat_product_policy("oc_policy")["bot_joined"] is True


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


def test_replay_shows_overdue_pending_approvals_without_mutation(tmp_path: Path, capsys) -> None:
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
    assert output["state"]["approvals"][0]["status"] == "pending"
    assert output["state"]["approvals"][0]["is_overdue"] is True
    assert output["state"]["approvals"][0]["overdue_seconds"] > 0
    assert output["state"]["approvals"][0]["recommended_action"] == "expire"
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_expired",),
        ).fetchone()
    assert approval["status"] == "pending"
    assert approval["resolved_at"] is None


def test_maintenance_expire_approvals_expires_overdue_and_reports_count(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_expire", "om_expire")
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_expire",
                task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_expire", "text": "reply"}),
                "reply",
                "2026-06-22T08:00:00+08:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )
        approval_id = int(cursor.lastrowid)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_expire",
        payload={"reply_target_message_id": "om_expire", "text": "reply", "identity": "user"},
        approval_id=approval_id,
    )
    assert action_id is not None

    assert main(["maintenance", "expire-approvals", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "applied"
    assert output["command"] == "maintenance.expire_approvals"
    assert output["actor"] == "local_cli"
    assert output["changed"] is True
    assert output["result"] == {"expired_approvals": 1}
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        action = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert approval["status"] == "expired"
    assert approval["resolved_at"] is not None
    assert action["status"] == "cancelled"


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
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a_dispatch_overdue",
                task_id,
                "send_reply",
                "pending",
                json.dumps({"reply_target_message_id": "om_root", "text": "reply"}),
                "reply",
                "2026-06-22T08:00:00+08:00",
                "2000-01-01T00:00:00+00:00",
            ),
        )

    assert main(["dispatch", "inspect", "--config", str(config), "--action-id", str(action_id)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "no_change"
    assert output["command"] == "dispatch.inspect"
    assert output["changed"] is False
    assert output["result"]["action"]["id"] == action_id
    assert output["result"]["action"]["status"] == "sending"
    assert output["result"]["attempts"][0]["status"] == "started"
    action = store.get_action(action_id)
    assert action is not None
    assert action.status == "sending"
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_dispatch_overdue",),
        ).fetchone()
    assert approval["status"] == "pending"
    assert approval["resolved_at"] is None


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
    assert output["status"] == "applied"
    assert output["command"] == "dispatch.retry"
    assert output["changed"] is True
    assert output["result"]["action"]["status"] == "pending"
    assert output["next_actions"] == [
        {"command": "dispatch.inspect", "target": {"type": "dispatch_action", "action_id": action_id}}
    ]
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

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "validation_failed"
    assert "only accepts failed or failed_needs_review" in output["result"]["error"]
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
    assert output["status"] == "applied"
    assert output["command"] == "dispatch.cancel"
    assert output["changed"] is True
    assert output["result"]["action"]["status"] == "cancelled"
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
    assert output["status"] == "applied"
    assert output["command"] == "dispatch.mark_sent"
    assert output["changed"] is True
    assert output["result"]["status"] == "sent"
    assert output["result"]["sent_message_id"] == "om_sent"
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
    assert output["status"] == "validation_failed"
    assert expected_error in output["result"]["error"]
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
    assert output["status"] == "conflict"
    assert "cancelled actions cannot be marked sent" in output["result"]["error"]
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
