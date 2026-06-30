from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.config import AppConfig, ChatPolicyConfig, OwnerConfig, ReplyPolicyConfig
from feishu_shadow_agent.operator_query import OperatorQueryService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _config(
    *,
    reply_policy: ReplyPolicyConfig | None = None,
    chats: dict[str, ChatPolicyConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        owner=OwnerConfig(open_id="ou_owner", name="Owner"),
        reply_policy=reply_policy or ReplyPolicyConfig(),
        chats=chats or {},
    )


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "agent.sqlite3")


def _insert_task(
    store: SQLiteStore,
    short_id: str = "t_1",
    *,
    chat_id: str = "oc_1",
    chat_type: str = "group",
    root_message_id: str = "om_root",
) -> int:
    store.migrate()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(
              short_id, status, chat_id, chat_type, root_message_id, task_label,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                "watching",
                chat_id,
                chat_type,
                root_message_id,
                "query label",
                "2026-06-22T08:00:00+08:00",
                "2026-06-22T08:00:00+08:00",
            ),
        )
    return int(cursor.lastrowid)


def _insert_message(store: SQLiteStore, *, task_id: int, message_id: str = "om_root") -> None:
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(
              message_id, chat_id, chat_type, sender_id, sender_role, sent_at, text,
              raw_json, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                "oc_1",
                "group",
                "ou_ext",
                "external_user_message",
                "2026-06-22T08:00:00+08:00",
                "hello",
                json.dumps({"message_id": message_id}),
                "2026-06-22T08:00:00+08:00",
            ),
        )
        conn.execute(
            "INSERT INTO task_messages(task_id, message_id, role, created_at) VALUES (?, ?, ?, ?)",
            (task_id, message_id, "root", "2026-06-22T08:00:00+08:00"),
        )


def _insert_approval(
    store: SQLiteStore,
    *,
    task_id: int,
    short_id: str = "a_1",
    expires_at: str = "2999-01-01T00:00:00+00:00",
    payload: dict[str, object] | None = None,
) -> int:
    store.migrate()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO approvals(
              short_id, task_id, kind, status, payload_json, preview, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                task_id,
                "send_reply",
                "pending",
                json.dumps(payload or {"reply_target_message_id": "om_root", "text": "reply"}),
                "reply",
                "2026-06-22T08:00:00+08:00",
                expires_at,
            ),
        )
    return int(cursor.lastrowid)


def test_dashboard_snapshot_includes_policy_status_and_omits_policy_audits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store.import_product_policy_from_config(config)
    query = OperatorQueryService(
        store,
        policy_import_source=config,
        now=lambda: "2026-06-22T10:00:00+08:00",
    )

    snapshot = query.dashboard_snapshot()

    assert snapshot["daemon_liveness"]["status"] == "not_started"
    assert snapshot["policy_status"]["initialized"] is True
    assert snapshot["policy_status"]["chat_policy_count"] == 1
    assert snapshot["policy_status"]["policy_import_diff"]["status"] == "matches"
    assert "policy_audits" not in snapshot
    json.dumps(snapshot)


def test_operator_query_derives_overdue_approval_without_mutating_db(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    _insert_approval(
        store,
        task_id=task_id,
        short_id="a_overdue",
        expires_at="2026-06-22T09:00:00+08:00",
    )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    detail = query.approval_detail("a_overdue")

    assert detail is not None
    assert detail["status"] == "pending"
    assert detail["is_overdue"] is True
    assert detail["overdue_seconds"] == 3600
    assert detail["recommended_action"] == "expire"
    assert detail["available_commands"] == ["maintenance expire-approvals"]
    with store.connect() as conn:
        row = conn.execute("SELECT status, resolved_at FROM approvals WHERE short_id = ?", ("a_overdue",)).fetchone()
    assert row["status"] == "pending"
    assert row["resolved_at"] is None


def test_approval_available_commands_respect_approvable_payload_without_exposing_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    _insert_approval(
        store,
        task_id=task_id,
        short_id="a_manual",
        payload={
            "reply_target_message_id": "om_root",
            "text": "",
            "approvable": False,
        },
    )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    detail = query.approval_detail("a_manual")
    listed = query.list_approvals(status="pending")

    assert detail is not None
    assert detail["available_commands"] == ["reject a_manual", "send t_1 <final_reply>"]
    assert "payload" not in listed[0]


def test_task_detail_returns_related_read_models_and_effective_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = _config(
        chats={
            "oc_1": ChatPolicyConfig(
                auto_reply=True,
                bot_joined=True,
                reply_identity="bot",
                allow_user_fallback=False,
                resource_download=False,
            )
        }
    )
    store.import_product_policy_from_config(config)
    task_id = _insert_task(store)
    _insert_message(store, task_id=task_id)
    _insert_approval(store, task_id=task_id, short_id="a_review")
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "bot"},
    )
    assert action_id is not None
    query = OperatorQueryService(
        store,
        policy_import_source=config,
        now=lambda: "2026-06-22T10:00:00+08:00",
    )

    detail = query.task_detail("t_1")

    assert detail is not None
    assert detail["task_id"] == "t_1"
    assert detail["message_count"] == 1
    assert [message["message_id"] for message in detail["recent_messages"]] == ["om_root"]
    assert detail["pending_approvals"][0]["approval_id"] == "a_review"
    assert detail["actions"][0]["action_id"] == action_id
    assert detail["effective_policy"] == {
        "policy_source": "explicit_chat",
        "auto_reply": True,
        "bot_joined": True,
        "reply_identity": "bot",
        "allow_user_fallback": False,
        "resource_download": False,
    }
    assert detail["recommended_actions"] == ["review_pending_approvals"]
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", ("a_review",)).fetchone()
    assert approval["status"] == "pending"


def test_dispatch_action_detail_reads_attempts_without_recovering_stale_sends(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    action_id = store.create_send_reply_action(
        task_id=task_id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "reply", "identity": "user"},
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id, run_id="run_1") is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE actions SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", action_id),
        )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    detail = query.dispatch_action_detail(action_id)

    assert detail is not None
    assert detail["action"]["status"] == "sending"
    assert detail["attempts"][0]["status"] == "started"
    assert detail["readback_summary"] == {
        "attempt_count": 1,
        "latest_status": "started",
        "sent_message_id": None,
        "error_stage": None,
        "readback_ok": False,
        "readback_message_id": None,
    }
    assert store.get_action(action_id).status == "sending"  # type: ignore[union-attr]


def test_policy_import_diff_and_audit_history_are_focused_read_models(tmp_path: Path) -> None:
    store = _store(tmp_path)
    initial = _config(chats={"oc_replace": ChatPolicyConfig(name="Before", auto_reply=True)})
    replacement = _config(
        reply_policy=ReplyPolicyConfig(p2p_auto_reply=False),
        chats={"oc_replace": ChatPolicyConfig(name="After", auto_reply=False)},
    )
    store.import_product_policy_from_config(initial)
    query = OperatorQueryService(store, policy_import_source=replacement)

    policy_status = query.policy_status()

    diff = policy_status["policy_import_diff"]
    assert diff["status"] == "differs"
    assert diff["changed_global"] is True
    assert diff["changed_chats"] == ["oc_replace"]
    assert "drift" not in diff["message"].lower()

    store.import_product_policy_from_config(replacement, replace=True)
    history = OperatorQueryService(store, policy_import_source=replacement).policy_audit_history(
        scope="chat",
        policy_key="chat:oc_replace",
        limit=1,
        offset=0,
    )

    assert len(history) == 1
    assert history[0]["scope"] == "chat"
    assert history[0]["policy_key"] == "chat:oc_replace"
    assert history[0]["old_summary"]["auto_reply"] is True
    assert history[0]["new_summary"]["auto_reply"] is False
