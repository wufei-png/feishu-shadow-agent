from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    OwnerConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.operator_commands import (
    OperatorCommandService,
    command_exit_code,
)
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
            (
                short_id,
                "watching",
                "oc_1",
                root_message_id,
                "label",
                "now",
                "now",
                "p2p",
            ),
        )
    return int(cursor.lastrowid)


def _create_pending_reply_approval(
    store: SQLiteStore, *, task_id: int, text: str
) -> str:
    approval_id = store.create_send_reply_approval(
        task_id=task_id,
        preview=text,
        payload={
            "reply_target_message_id": "om_root",
            "text": text,
            "identity": "user",
            "source": "approval_request",
            "decision_reason": "commitment_or_authorization",
        },
        approval_timeout_hours=None,
    )
    with store.connect() as conn:
        return conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()["short_id"]


def _config(
    *,
    reply_policy: ReplyPolicyConfig | None = None,
    chats: dict[str, ChatPolicyConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        reply_policy=reply_policy or ReplyPolicyConfig(),
        chats=chats or {},
    )


def test_operator_command_service_send_returns_stable_result_shape(
    tmp_path: Path,
) -> None:
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
        action = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
    payload = json.loads(action["payload_json"])
    assert payload["text"] == "operator final reply"
    assert payload["source"] == "owner_send"


@pytest.mark.parametrize(
    ("operation", "expected_outcome", "expected_final", "expected_status"),
    [
        ("approve", "suggestion_sent", "suggested reply", "watching"),
        ("edit", "edited_sent", "edited reply", "watching"),
        ("keep", "no_send_keep_watching", None, "watching"),
        ("end", "no_send_end_task", None, "closed"),
    ],
)
def test_approval_resolution_atomically_records_immutable_feedback(
    tmp_path: Path,
    operation: str,
    expected_outcome: str,
    expected_final: str | None,
    expected_status: str,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_feedback", "om_root")
    approval_id = _create_pending_reply_approval(
        store, task_id=task_id, text="suggested reply"
    )
    service = OperatorCommandService(
        store, keep_watching_until_factory=lambda: "2026-08-14T00:00:00+08:00"
    )

    if operation == "approve":
        result = service.approve(
            approval_id,
            actor="owner",
            command_id="evt_approve",
            note="looks good",
        )
    elif operation == "edit":
        result = service.send(
            approval_id,
            "edited reply",
            actor="owner",
            command_id="evt_edit",
            feedback_reason="tone_or_style",
        )
    else:
        result = service.do_not_send(
            approval_id,
            keep_watching=operation == "keep",
            actor="owner",
            command_id=f"evt_{operation}",
            feedback_reason="unnecessary_reply",
        )

    duplicate = service.approve(
        approval_id,
        actor="owner",
        command_id=f"evt_{operation}",
    )
    with store.connect() as conn:
        feedback = conn.execute("SELECT * FROM approval_feedback").fetchone()
        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM approval_feedback").fetchone()[0]

    assert result.status == "applied"
    assert result.result["outcome"] == expected_outcome
    assert duplicate.status == "no_change"
    assert count == 1
    assert feedback["outcome"] == expected_outcome
    assert feedback["decision_reason"] == "commitment_or_authorization"
    assert feedback["suggested_reply"] == "suggested reply"
    assert feedback["final_reply"] == expected_final
    assert feedback["execution_mode"] == "production"
    assert task["status"] == expected_status


def test_dry_run_approval_requires_new_production_approval_for_real_send(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_mode", "om_root")
    dry_approval = _create_pending_reply_approval(
        store, task_id=task_id, text="same reply"
    )
    service = OperatorCommandService(store)

    dry_result = service.approve(
        dry_approval,
        actor="owner",
        command_id="evt_dry",
        execution_mode="dry_run",
    )
    production_approval = _create_pending_reply_approval(
        store, task_id=task_id, text="same reply"
    )
    production_result = service.approve(
        production_approval,
        actor="owner",
        command_id="evt_prod",
        execution_mode="production",
    )

    with store.connect() as conn:
        modes = [
            row["execution_mode"]
            for row in conn.execute(
                "SELECT execution_mode FROM actions WHERE kind = 'send_reply' ORDER BY id"
            ).fetchall()
        ]

    assert dry_result.status == "applied"
    assert production_result.status == "applied"
    assert modes == ["dry_run", "production"]


def test_operator_command_service_approval_command_id_replay_returns_no_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _insert_task(store, "t_cmd_id", "om_root")
    service = OperatorCommandService(store)

    first = service.send(
        "t_cmd_id", "same reply", actor="test_operator", command_id="cmd_same"
    )
    second = service.send(
        "t_cmd_id", "same reply", actor="test_operator", command_id="cmd_same"
    )

    assert first.status == "applied"
    assert second.status == "no_change"
    assert second.changed is False
    assert second.result["approval_command_status"] == "duplicate"
    with store.connect() as conn:
        action_count = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'"
        ).fetchone()["c"]
    assert action_count == 1


@pytest.mark.parametrize("verb", ["approve", "reject", "send"])
def test_operator_command_service_task_shortcut_conflict_is_structured(
    tmp_path: Path, verb: str
) -> None:
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
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'"
        ).fetchone()["c"]
        send_actions = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'"
        ).fetchone()["c"]
    assert pending == 2
    assert send_actions == 0


def test_operator_command_service_dispatch_retry_reports_validation_without_argparse(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_retry", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "reply",
            "identity": "user",
        },
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id, run_id="run_1") is not None

    result = OperatorCommandService(store).retry_dispatch_action(
        action_id, actor="test_operator"
    )
    output = result.as_dict()

    assert command_exit_code(result) == 2
    assert output["status"] == "validation_failed"
    assert output["command"] == "dispatch.retry"
    assert output["changed"] is False
    assert "only accepts failed or failed_needs_review" in output["result"]["error"]


def test_operator_command_service_cancel_sent_action_reports_conflict(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_sent", "om_root")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "reply",
            "identity": "user",
        },
    )
    assert action_id is not None
    store.finish_action(action_id, status="sent", result={"sent_message_id": "om_sent"})

    result = OperatorCommandService(store).cancel_dispatch_action(
        action_id, actor="test_operator"
    )
    output = result.as_dict()

    assert command_exit_code(result) == 2
    assert output["status"] == "conflict"
    assert output["changed"] is False
    assert "sent actions cannot be cancelled" in output["result"]["error"]


def test_operator_command_service_expire_approvals_reports_no_change(
    tmp_path: Path,
) -> None:
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


def test_operator_command_service_close_and_reopen_task_lifecycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_lifecycle", "om_root")
    approval_id = store.create_send_reply_approval(
        task_id=task_id,
        preview="draft",
        payload={
            "reply_target_message_id": "om_root",
            "text": "draft",
            "identity": "user",
        },
        approval_timeout_hours=None,
    )
    send_action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "draft",
            "identity": "user",
        },
        approval_id=approval_id,
    )
    notification_action_id = store.create_owner_notification_action(
        task_id=task_id,
        payload={"type": "needs_attention"},
    )
    assert send_action_id is not None
    assert notification_action_id is not None
    service = OperatorCommandService(store)

    closed = service.close_task(
        "t_lifecycle", actor="test_operator", reason="not needed"
    )

    assert command_exit_code(closed) == 0
    assert closed.as_dict()["status"] == "applied"
    assert closed.as_dict()["command"] == "task.close"
    assert closed.as_dict()["reason"] == "not needed"
    assert closed.as_dict()["result"]["expired_approvals"] == 1
    assert closed.as_dict()["result"]["cancelled_actions"] == 2
    with store.connect() as conn:
        task = conn.execute(
            "SELECT status, closed_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        actions = conn.execute(
            "SELECT status FROM actions WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    assert task["status"] == "closed_by_owner"
    assert task["closed_at"] is not None
    assert approval["status"] == "expired"
    assert approval["resolved_at"] is not None
    assert [row["status"] for row in actions] == ["cancelled", "cancelled"]

    closed_again = service.close_task("t_lifecycle", actor="test_operator")
    reopened = service.reopen_task(
        "t_lifecycle",
        watch_until="2026-06-22T12:00:00+08:00",
        actor="test_operator",
        reason="resume",
    )

    assert closed_again.status == "no_change"
    assert reopened.status == "applied"
    assert reopened.as_dict()["command"] == "task.reopen"
    with store.connect() as conn:
        task = conn.execute(
            "SELECT status, closed_at, watch_until FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert task["status"] == "watching"
    assert task["closed_at"] is None
    assert task["watch_until"] == "2026-06-22T12:00:00+08:00"


def test_operator_command_service_close_missing_task_reports_not_found(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    result = OperatorCommandService(store).close_task(
        "t_missing", actor="test_operator"
    )

    assert command_exit_code(result) == 2
    assert result.status == "not_found"
    assert result.changed is False


def test_operator_command_service_policy_import_uses_facade_result_shape(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OperatorCommandService(store)

    result = service.import_policy_config(
        _config(),
        used_defaults=True,
        actor="test_operator",
        reason="initial seed",
    )
    output = result.as_dict()

    assert command_exit_code(result) == 0
    assert output["status"] == "applied"
    assert output["command"] == "policy.import_config"
    assert output["actor"] == "test_operator"
    assert output["reason"] == "initial seed"
    assert output["changed"] is True
    assert output["result"]["inserted"]["global"] == ["reply_policy"]
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert output["policy_import_diff"]["status"] == "matches"
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "test_operator"
    assert audit["reason"] == "initial seed"


def test_global_policy_expansion_applies_directly_with_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.import_product_policy_from_config(
        _config(
            reply_policy=ReplyPolicyConfig(
                p2p_auto_reply=False, unknown_group_auto_reply=False
            )
        )
    )
    service = OperatorCommandService(store)

    result = service.update_global_policy(
        {"p2p_auto_reply": True},
        actor="test_operator",
        reason="enable p2p",
    )
    output = result.as_dict()

    assert command_exit_code(result) == 0
    assert output["status"] == "applied"
    assert output["changed"] is True
    assert output["warnings"] == []
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert store.get_product_policy()["reply_policy"]["p2p_auto_reply"] is True
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "test_operator"
    assert audit["reason"] == "enable p2p"
    assert audit["old_json"]["reply_policy"]["p2p_auto_reply"] is False
    assert audit["new_json"]["reply_policy"]["p2p_auto_reply"] is True


def test_chat_policy_expansion_and_narrowing_updates_are_structured(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.import_product_policy_from_config(
        _config(
            chats={
                "oc_policy": ChatPolicyConfig(
                    name="Policy group",
                    auto_reply=False,
                    resource_download=True,
                )
            }
        )
    )
    service = OperatorCommandService(store)

    expansion = service.update_chat_policy(
        "oc_policy",
        {"auto_reply": True},
        actor="test_operator",
        reason="open chat",
    )
    expansion_output = expansion.as_dict()

    assert command_exit_code(expansion) == 0
    assert expansion.status == "applied"
    assert expansion.changed is True
    assert expansion_output["warnings"] == []
    assert expansion_output["old_policy"]["auto_reply"] is False
    assert expansion_output["new_policy"]["auto_reply"] is True
    assert "risk_level" not in expansion_output
    assert "confirmation_required" not in expansion_output
    assert store.get_chat_product_policy("oc_policy")["auto_reply"] is True

    narrowing = service.update_chat_policy(
        "oc_policy",
        {"resource_download": False},
        actor="test_operator",
        reason="narrow resources",
    )
    output = narrowing.as_dict()

    assert command_exit_code(narrowing) == 0
    assert output["status"] == "applied"
    assert output["warnings"] == []
    assert "risk_level" not in output
    assert "confirmation_required" not in output
    assert output["audit_count"] == 1
    assert store.get_chat_product_policy("oc_policy")["resource_download"] is False
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "test_operator"
    assert audit["reason"] == "narrow resources"


def test_chat_policy_update_requires_initialized_global_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = OperatorCommandService(store)

    result = service.update_chat_policy(
        "oc_missing_global",
        {"auto_reply": False},
        actor="test_operator",
    )

    assert command_exit_code(result) == 2
    assert result.as_dict()["status"] == "not_found"
    assert "policy import-config" in result.as_dict()["result"]["error"]
    assert store.get_chat_product_policy("oc_missing_global") is None


def test_chat_policy_delete_removes_override_without_global_policy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.upsert_chat_product_policy(
        {
            "chat_id": "oc_delete",
            "name": "Delete me",
            "auto_reply": True,
            "bot_joined": False,
            "reply_identity": "bot_preferred",
            "allow_user_fallback": True,
            "resource_download": True,
        },
        actor="seed",
    )
    service = OperatorCommandService(store)

    result = service.delete_chat_policy(
        "  oc_delete  ",
        actor="test_operator",
        reason="remove override",
    )
    output = result.as_dict()

    assert command_exit_code(result) == 0
    assert output["status"] == "applied"
    assert output["command"] == "policy.delete_chat"
    assert output["target"] == {"type": "chat_policy", "chat_id": "oc_delete"}
    assert output["old_policy"]["auto_reply"] is True
    assert output["new_policy"] is None
    assert output["audit_count"] == 1
    assert store.get_product_policy() is None
    assert store.get_chat_product_policy("oc_delete") is None
    audit = store.list_policy_audits(limit=1)[0]
    assert audit["actor"] == "test_operator"
    assert audit["reason"] == "remove override"
    assert audit["new_json"] is None


def test_chat_policy_delete_missing_policy_returns_not_found(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = OperatorCommandService(store)

    result = service.delete_chat_policy("oc_missing", actor="test_operator")

    assert command_exit_code(result) == 2
    assert result.as_dict()["status"] == "not_found"
    assert "oc_missing" in result.as_dict()["result"]["error"]
    assert store.list_policy_audits(limit=10) == []


def test_chat_policy_update_normalizes_chat_id_before_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.import_product_policy_from_config(
        _config(
            chats={
                "oc_policy": ChatPolicyConfig(
                    name="Policy group",
                    auto_reply=True,
                    bot_joined=True,
                    resource_download=True,
                )
            }
        )
    )
    service = OperatorCommandService(store)

    result = service.update_chat_policy(
        "  oc_policy  ",
        {"resource_download": False},
        actor="test_operator",
        reason="narrow resources",
    )

    assert command_exit_code(result) == 0
    assert result.as_dict()["target"] == {"type": "chat_policy", "chat_id": "oc_policy"}
    policy = store.get_chat_product_policy("oc_policy")
    assert policy["name"] == "Policy group"
    assert policy["auto_reply"] is True
    assert policy["bot_joined"] is True
    assert policy["resource_download"] is False


def test_chat_policy_effective_capability_expansion_applies_directly(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.import_product_policy_from_config(
        _config(
            chats={
                "oc_bot_joined": ChatPolicyConfig(
                    name="Bot joined",
                    auto_reply=True,
                    bot_joined=False,
                    reply_identity="bot_preferred",
                    resource_download=True,
                ),
                "oc_bot_preferred": ChatPolicyConfig(
                    name="Bot preferred",
                    auto_reply=True,
                    bot_joined=False,
                    reply_identity="bot",
                    allow_user_fallback=True,
                    resource_download=False,
                ),
            }
        )
    )
    service = OperatorCommandService(store)

    bot_joined = service.update_chat_policy(
        "oc_bot_joined",
        {"bot_joined": True},
        actor="test_operator",
        reason="bot available",
    )
    bot_preferred = service.update_chat_policy(
        "oc_bot_preferred",
        {"reply_identity": "bot_preferred"},
        actor="test_operator",
        reason="allow fallback",
    )

    assert bot_joined.status == "applied"
    assert bot_joined.warnings == []
    assert bot_joined.as_dict()["audit_count"] == 1
    assert bot_preferred.status == "applied"
    assert bot_preferred.warnings == []
    assert bot_preferred.as_dict()["audit_count"] == 1
    assert store.get_chat_product_policy("oc_bot_joined")["bot_joined"] is True
    assert (
        store.get_chat_product_policy("oc_bot_preferred")["reply_identity"]
        == "bot_preferred"
    )
