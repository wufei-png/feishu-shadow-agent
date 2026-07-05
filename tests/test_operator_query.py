from __future__ import annotations

import json
from pathlib import Path

from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    OwnerConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.operator_query import OperatorQueryService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HealthCheckResult


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


def _insert_message(
    store: SQLiteStore, *, task_id: int, message_id: str = "om_root"
) -> None:
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
                json.dumps(
                    payload or {"reply_target_message_id": "om_root", "text": "reply"}
                ),
                "reply",
                "2026-06-22T08:00:00+08:00",
                expires_at,
            ),
        )
    return int(cursor.lastrowid)


def test_dashboard_snapshot_includes_policy_status_and_omits_policy_audits(
    tmp_path: Path,
) -> None:
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


def test_operator_query_derives_overdue_approval_without_mutating_db(
    tmp_path: Path,
) -> None:
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
        row = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_overdue",),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["resolved_at"] is None


def test_approval_available_commands_respect_approvable_payload_without_exposing_payload(
    tmp_path: Path,
) -> None:
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


def test_approval_dto_exposes_postprocess_badge_fields_without_full_payload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    _insert_approval(
        store,
        task_id=task_id,
        short_id="a_postprocess",
        payload={
            "reply_target_message_id": "om_root",
            "text": "reply",
            "keep_watching_on_reject": True,
            "postprocess": {
                "applied": False,
                "status": "failed",
                "failure_reason": "profile_missing",
            },
        },
    )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    listed = query.list_approvals(status="pending")
    detail = query.approval_detail("a_postprocess")

    assert listed[0]["postprocess_status"] == "failed"
    assert listed[0]["postprocess_applied"] is False
    assert listed[0]["postprocess_failure_reason"] == "profile_missing"
    assert "payload" not in listed[0]
    assert detail is not None
    assert detail["payload"]["keep_watching_on_reject"] is True


def test_task_detail_returns_related_read_models_and_effective_policy(
    tmp_path: Path,
) -> None:
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
        payload={
            "reply_target_message_id": "om_root",
            "text": "reply",
            "identity": "bot",
        },
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
    assert [message["message_id"] for message in detail["recent_messages"]] == [
        "om_root"
    ]
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
    assert detail["recommended_actions"] == [
        "task close --task-id t_1",
        "review_pending_approvals",
    ]
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status FROM approvals WHERE short_id = ?", ("a_review",)
        ).fetchone()
    assert approval["status"] == "pending"


def test_task_and_dispatch_lists_include_recommended_actions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    _insert_approval(
        store,
        task_id=task_id,
        short_id="a_overdue_list",
        expires_at="2026-06-22T09:00:00+08:00",
    )
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
    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    tasks = query.list_tasks()
    actions = query.list_dispatch_actions()

    assert tasks[0]["recommended_actions"] == [
        "task close --task-id t_1",
        "expire_overdue_approvals",
        "inspect_failed_needs_review_actions",
    ]
    assert actions[0]["recommended_actions"] == [
        f"dispatch inspect --action-id {action_id}",
        f"dispatch mark-sent --action-id {action_id} --sent-message-id <message_id>",
        f"dispatch retry --action-id {action_id}",
        f"dispatch cancel --action-id {action_id}",
    ]


def test_dispatch_action_detail_reads_attempts_without_recovering_stale_sends(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
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


def test_message_detail_returns_processing_context_without_preview_side_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
    _insert_message(store, task_id=task_id)
    _insert_approval(store, task_id=task_id, short_id="a_msg_detail")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO routing_audits(message_id, task_id, route, route_reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("om_root", task_id, "new_task", "new task", "2026-06-22T08:00:00+08:00"),
        )
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
    before = _message_detail_state(store, action_id)
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    detail = query.message_detail("om_root")

    assert detail is not None
    assert detail["message"]["message_id"] == "om_root"
    assert detail["task_ids"] == [task_id]
    assert detail["task_summaries"][0]["task_id"] == "t_1"
    assert detail["routing_audits"][0]["route"] == "new_task"
    assert detail["approvals"][0]["approval_id"] == "a_msg_detail"
    assert detail["actions"][0]["action_id"] == action_id
    assert "payload" not in detail["approvals"][0]
    assert "payload" not in detail["actions"][0]
    assert detail["recorded_dispatch_outcomes"][0]["attempts"] == []
    assert detail["recommended_actions"] == ["review_pending_approvals"]
    assert _message_detail_state(store, action_id) == before


def test_policy_import_diff_and_audit_history_are_focused_read_models(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    initial = _config(
        chats={"oc_replace": ChatPolicyConfig(name="Before", auto_reply=True)}
    )
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
    history = OperatorQueryService(
        store, policy_import_source=replacement
    ).policy_audit_history(
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


def test_health_issues_reports_store_availability_without_migrating(
    tmp_path: Path,
) -> None:
    missing = OperatorQueryService(_store(tmp_path / "missing")).health_issues()

    empty_db = tmp_path / "empty" / "agent.sqlite3"
    empty_db.parent.mkdir()
    empty_db.write_bytes(b"")
    unmigrated = OperatorQueryService(SQLiteStore(empty_db)).health_issues()

    directory_db = tmp_path / "directory.sqlite3"
    directory_db.mkdir()
    unreadable = OperatorQueryService(SQLiteStore(directory_db)).health_issues()

    bad_schema_store = _store(tmp_path / "bad_schema")
    bad_schema_store.migrate()
    with bad_schema_store.connect() as conn:
        conn.execute("ALTER TABLE product_policies DROP COLUMN updated_at")
    schema_incompatible = OperatorQueryService(bad_schema_store).health_issues()

    assert missing["runtime"]["store"]["status"] == "missing"
    assert missing["issues"][0]["category"] == "store"
    assert missing["summary"]["highest_severity"] == "critical"
    assert unmigrated["runtime"]["store"]["status"] == "schema_uninitialized"
    assert unreadable["runtime"]["store"]["status"] == "unreadable"
    assert schema_incompatible["runtime"]["store"]["status"] == "schema_incompatible"
    assert schema_incompatible["issues"][0]["id"] == "store-schema_incompatible"


def test_health_issues_summary_count_is_not_truncated_by_list_limit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.migrate()
    task_id = _insert_task(store)
    for target_message_id in ("om_failed_1", "om_failed_2"):
        action_id = store.create_send_reply_action(
            task_id=task_id,
            target_message_id=target_message_id,
            payload={
                "reply_target_message_id": target_message_id,
                "text": "reply",
                "identity": "user",
            },
        )
        assert action_id is not None
        store.finish_action(action_id, status="failed", result={"error_stage": "send"})
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    payload = query.health_issues(limit=1)

    assert len(payload["issues"]) == 1
    assert payload["summary"]["open_issue_count"] == 4


def test_health_issues_only_counts_latest_health_check_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "hermes",
                "critical",
                "ok",
                "Hermes recovered",
                "{}",
                "2026-06-22T09:30:00+08:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                "hermes",
                "critical",
                "failed",
                "Hermes failed",
                "{}",
                "2026-06-22T09:00:00+08:00",
            ),
        )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    payload = query.health_issues()

    assert payload["summary"]["open_issue_count"] == 2
    assert {issue["id"] for issue in payload["issues"]} == {
        "policy-uninitialized",
        "daemon-not-started",
    }


def test_health_issues_redacts_failed_approval_command_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO approval_commands(message_id, command, status, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cmd_secret",
                "/send t_secret highly sensitive final reply",
                "failed",
                json.dumps({"error": "failed at /tmp/secret/log.jsonl"}),
                "2026-06-22T09:30:00+08:00",
                "2026-06-22T09:30:00+08:00",
            ),
        )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    payload = query.health_issues()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "highly sensitive final reply" not in serialized
    assert "/tmp/secret" not in serialized
    assert payload["recent_failed_commands"][0]["label"] == "/send t_secret"
    assert payload["recent_failed_commands"][0]["command"] == "/send t_secret"
    assert (
        payload["recent_failed_commands"][0]["result_summary"]["error"]
        == "failed at [path]"
    )
    assert payload["recent_failed_commands"][0]["verb"] == "send"
    assert payload["recent_failed_commands"][0]["target_id"] == "t_secret"
    assert payload["summary"]["open_issue_count"] == 2
    assert {issue["category"] for issue in payload["issues"]} == {"policy", "daemon"}


def test_health_issues_dispatch_cancel_removes_open_issue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
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
    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    before = query.health_issues()
    store.cancel_dispatch_action(action_id)
    after = query.health_issues()

    assert f"dispatch-action-{action_id}" in {issue["id"] for issue in before["issues"]}
    assert before["summary"]["open_issue_count"] == 3
    assert f"dispatch-action-{action_id}" not in {
        issue["id"] for issue in after["issues"]
    }
    assert after["summary"]["open_issue_count"] == 2


def test_health_issues_derives_actionable_runtime_and_recovery_issues(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store)
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
    store.finish_action(
        action_id, status="failed_needs_review", result={"error_stage": "send"}
    )
    store.record_run_tick_started(run_id="run_stale", dry_run=True)
    store.record_health_results(
        run_id="run_stale",
        results=[
            HealthCheckResult(
                name="hermes",
                severity="warning",
                status="failed",
                message="Hermes health failed at /tmp/secret/hermes.log",
            )
        ],
    )
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET last_heartbeat_at = ?,
                health_summary_json = ?,
                last_tick_summary_json = ?
            WHERE run_id = ?
            """,
            (
                "2026-06-22T09:00:00+08:00",
                json.dumps({"error": "doctor failed at /tmp/secret/doctor.log"}),
                json.dumps({"stage": "runtime", "log": "/tmp/secret/tick.log"}),
                "run_stale",
            ),
        )
        conn.execute(
            """
            INSERT INTO approval_commands(message_id, command, status, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cmd_failed",
                "/approve a_missing",
                "failed",
                json.dumps({"error": "approval not found"}),
                "2026-06-22T09:30:00+08:00",
                "2026-06-22T09:30:00+08:00",
            ),
        )
    query = OperatorQueryService(store, now=lambda: "2026-06-22T10:00:00+08:00")

    payload = query.health_issues()
    serialized = json.dumps(payload, ensure_ascii=False)

    issues = {issue["id"]: issue for issue in payload["issues"]}
    assert payload["runtime"]["store"]["status"] == "available"
    assert payload["runtime"]["daemon_liveness"]["status"] == "stale"
    assert "health_summary_json" not in payload["runtime"]["last_run"]
    assert "last_tick_summary_json" not in payload["runtime"]["last_run"]
    assert "/tmp/secret" not in serialized
    assert payload["summary"]["highest_severity"] == "critical"
    assert issues["policy-uninitialized"]["category"] == "policy"
    assert issues["daemon-stale"]["severity"] == "error"
    assert issues[f"dispatch-action-{action_id}"]["recommended_actions"] == [
        "inspect",
        "mark_sent",
        "retry",
        "cancel",
    ]
    assert all(issue["category"] != "approval_command" for issue in payload["issues"])
    hermes_issue = next(
        issue for issue in payload["issues"] if issue["id"].startswith("health-hermes-")
    )
    assert "/tmp/secret" not in hermes_issue["detail"]
    assert "[path]" in hermes_issue["detail"]
    assert payload["recent_failed_dispatch_actions"][0]["action_id"] == action_id
    assert payload["recent_failed_commands"][0]["message_id"] == "cmd_failed"


def _message_detail_state(store: SQLiteStore, action_id: int) -> dict[str, object]:
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE short_id = ?",
            ("a_msg_detail",),
        ).fetchone()
        action = conn.execute(
            "SELECT status, result_json FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        attempts = conn.execute(
            "SELECT COUNT(*) AS count FROM dispatch_attempts WHERE action_id = ?",
            (action_id,),
        ).fetchone()
    return {
        "approval_status": approval["status"],
        "approval_resolved_at": approval["resolved_at"],
        "action_status": action["status"],
        "action_result_json": action["result_json"],
        "attempt_count": attempts["count"],
    }
