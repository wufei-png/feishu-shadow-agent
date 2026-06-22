from __future__ import annotations

import io
import json
from pathlib import Path

import yaml

from feishu_shadow_agent.cli import main
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


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
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sent_at, text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "om_1",
                "oc_1",
                "p2p",
                "ou_a",
                "2026-06-22T10:00:00+08:00",
                "hello",
                json.dumps({"message_id": "om_1"}),
                "now",
            ),
        )

    assert main(["replay", "--config", str(config), "--message-id", "om_1", "--dry-run"]) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["message_id"] == "om_1"
    assert output["mutated_real_db"] is False
    with store.connect() as conn:
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert action_count == 0
