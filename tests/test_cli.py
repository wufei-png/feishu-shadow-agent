from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult


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


def test_status_includes_failed_approval_commands(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    assert main(["reject", "--config", str(config), "a_missing"]) == 2
    capsys.readouterr()

    assert main(["status", "--config", str(config)]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["failed_approval_commands"][0]["status"] == "failed"
    assert output["failed_approval_commands"][0]["command"] == "/reject a_missing"


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
