from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_shadow_agent.operator_commands import OperatorCommandService, command_exit_code
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


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


def _create_pending_reply_approval(store: SQLiteStore, *, task_id: int, text: str) -> str:
    approval_id = store.create_send_reply_approval(
        task_id=task_id,
        preview=text,
        payload={
            "reply_target_message_id": "om_root",
            "text": text,
            "identity": "user",
            "source": "approval_request",
        },
        approval_timeout_hours=None,
    )
    with store.connect() as conn:
        return conn.execute("SELECT short_id FROM approvals WHERE id = ?", (approval_id,)).fetchone()["short_id"]


def test_operator_command_service_send_returns_stable_result_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_cmd", "om_root")

    result = OperatorCommandService(store).send(
        "t_cmd",
        "operator final reply",
        actor="test_operator",
        reason="manual_resolution",
    )
    output = result.as_dict()

    assert command_exit_code(result) == 0
    assert list(output) == [
        "status",
        "command",
        "actor",
        "reason",
        "target",
        "changed",
        "result",
        "warnings",
        "next_actions",
    ]
    assert output["status"] == "applied"
    assert output["command"] == "approval.send"
    assert output["actor"] == "test_operator"
    assert output["reason"] == "manual_resolution"
    assert output["target"] == {"type": "approval_or_task", "id": "t_cmd"}
    assert output["changed"] is True
    assert output["result"]["task_id"] == task_id
    assert output["result"]["approval_command_status"] == "applied"
    assert output["next_actions"][0]["command"] == "dispatch.inspect"

    with store.connect() as conn:
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
    payload = json.loads(action["payload_json"])
    assert payload["text"] == "operator final reply"
    assert payload["source"] == "owner_send"


def test_operator_command_service_approval_command_id_replay_returns_no_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_task(store, "t_cmd_id", "om_root")
    service = OperatorCommandService(store)

    first = service.send("t_cmd_id", "same reply", actor="test_operator", command_id="cmd_same")
    second = service.send("t_cmd_id", "same reply", actor="test_operator", command_id="cmd_same")

    assert first.status == "applied"
    assert second.status == "no_change"
    assert second.changed is False
    assert second.result["approval_command_status"] == "duplicate"
    with store.connect() as conn:
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert action_count == 1


@pytest.mark.parametrize("verb", ["approve", "reject", "send"])
def test_operator_command_service_task_shortcut_conflict_is_structured(tmp_path: Path, verb: str) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_multi", "om_root")
    _create_pending_reply_approval(store, task_id=task_id, text="first")
    _create_pending_reply_approval(store, task_id=task_id, text="second")
    service = OperatorCommandService(store)

    if verb == "approve":
        result = service.approve("t_multi", actor="test_operator")
    elif verb == "reject":
        result = service.reject("t_multi", actor="test_operator")
    else:
        result = service.send("t_multi", "final", actor="test_operator")
    output = result.as_dict()

    assert command_exit_code(result) == 2
    assert output["status"] == "conflict"
    assert output["changed"] is False
    assert output["result"]["approval_command_status"] == "failed"
    assert len(output["result"]["pending_approval_ids"]) == 2
    assert output["result"]["notification_action_id"] is not None
    with store.connect() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert pending == 2
    assert send_actions == 0


def test_operator_command_service_dispatch_retry_reports_validation_without_argparse(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_retry", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id, run_id="run_1") is not None

    result = OperatorCommandService(store).retry_dispatch_action(action_id, actor="test_operator")
    output = result.as_dict()

    assert command_exit_code(result) == 2
    assert output["status"] == "validation_failed"
    assert output["command"] == "dispatch.retry"
    assert output["changed"] is False
    assert "only accepts failed or failed_needs_review" in output["result"]["error"]


def test_operator_command_service_cancel_sent_action_reports_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_sent", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    store.finish_action(action_id, status="sent", result={"sent_message_id": "om_sent"})

    result = OperatorCommandService(store).cancel_dispatch_action(action_id, actor="test_operator")
    output = result.as_dict()

    assert command_exit_code(result) == 2
    assert output["status"] == "conflict"
    assert output["changed"] is False
    assert "sent actions cannot be cancelled" in output["result"]["error"]


def test_operator_command_service_expire_approvals_reports_no_change(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = OperatorCommandService(store).expire_approvals(actor="test_operator")

    assert command_exit_code(result) == 0
    assert result.as_dict() == {
        "status": "no_change",
        "command": "maintenance.expire_approvals",
        "actor": "test_operator",
        "reason": None,
        "target": {"type": "approval_queue"},
        "changed": False,
        "result": {"expired_approvals": 0},
        "warnings": [],
        "next_actions": [],
    }
