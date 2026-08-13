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


def _insert_feedback(
    store: SQLiteStore,
    *,
    task_id: int,
    suffix: str,
    outcome: str,
    decision_reason: str,
    created_at: str,
    suggested_reply: str | None = "suggested reply",
    final_reply: str | None = None,
    feedback_reason: str | None = None,
    execution_mode: str = "production",
    content_expired_at: str | None = None,
) -> None:
    approval_id = _insert_approval(
        store,
        task_id=task_id,
        short_id=f"a_{suffix}",
        payload={
            "reply_target_message_id": "om_root",
            "text": suggested_reply or "",
            "decision_reason": decision_reason,
        },
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'approved', resolved_at = ? WHERE id = ?",
            (created_at, approval_id),
        )
        conn.execute(
            """
            INSERT INTO approval_feedback(
              approval_id, task_id, command_id, outcome, decision_reason,
              suggested_reply, final_reply, feedback_reason, note, actor,
              execution_mode, content_expired_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                task_id,
                f"cmd_{suffix}",
                outcome,
                decision_reason,
                suggested_reply,
                final_reply,
                feedback_reason,
                "note" if content_expired_at is None else None,
                "ou_owner",
                execution_mode,
                content_expired_at,
                created_at,
            ),
        )


def test_feedback_overview_has_7_and_30_day_metrics_and_reply_diff(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_feedback")
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="exact",
        outcome="suggestion_sent",
        decision_reason="commitment_or_authorization",
        created_at="2026-08-12T10:00:00+08:00",
        final_reply="suggested reply",
    )
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="edited",
        outcome="edited_sent",
        decision_reason="human_judgment_required",
        created_at="2026-08-11T10:00:00+08:00",
        suggested_reply="I can Friday",
        final_reply="I can do it Friday",
        feedback_reason="tone_or_style",
    )
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="nosend",
        outcome="no_send_keep_watching",
        decision_reason="insufficient_evidence",
        created_at="2026-08-10T10:00:00+08:00",
        final_reply=None,
    )
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="older",
        outcome="edited_sent",
        decision_reason="commitment_or_authorization",
        created_at="2026-08-01T10:00:00+08:00",
        suggested_reply=None,
        final_reply=None,
        content_expired_at="2026-08-12T00:00:00+08:00",
    )
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="dry",
        outcome="no_send_end_task",
        decision_reason="write_or_permission",
        created_at="2026-08-12T11:00:00+08:00",
        execution_mode="dry_run",
    )
    query = OperatorQueryService(store, now=lambda: "2026-08-13T12:00:00+08:00")

    overview = query.feedback_overview()

    seven, thirty = overview["windows"]
    assert overview["execution_mode"] == "production"
    assert seven["days"] == 7
    assert seven["total"] == 3
    assert seven["sent_without_edit_rate"] == 0.5
    assert seven["edit_rate_among_sends"] == 0.5
    assert seven["no_send_rate"] == 0.3333
    assert seven["changed_reply_count"] == 1
    assert thirty["days"] == 30
    assert thirty["total"] == 4
    assert thirty["content_expired_count"] == 1
    assert {item["value"] for item in thirty["by_decision_reason"]} == {
        "commitment_or_authorization",
        "human_judgment_required",
        "insufficient_evidence",
    }
    edited = next(
        item for item in overview["recent"] if item["approval_id"] == "a_edited"
    )
    assert edited["reply_comparison"]["status"] == "changed"
    assert edited["reply_comparison"]["suggested_reply"] == "I can Friday"
    assert edited["reply_comparison"]["final_reply"] == "I can do it Friday"
    assert {part["op"] for part in edited["reply_comparison"]["diff"]} >= {
        "equal",
        "insert",
    }
    older = next(
        item for item in overview["recent"] if item["approval_id"] == "a_older"
    )
    assert older["reply_comparison"] == {
        "status": "expired",
        "suggested_reply": None,
        "final_reply": None,
        "diff": [],
    }


def test_feedback_query_can_explicitly_include_dry_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _insert_task(store, "t_feedback_modes")
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="prod",
        outcome="suggestion_sent",
        decision_reason="commitment_or_authorization",
        created_at="2026-08-12T10:00:00+08:00",
        final_reply="suggested reply",
    )
    _insert_feedback(
        store,
        task_id=task_id,
        suffix="dry_mode",
        outcome="no_send_end_task",
        decision_reason="write_or_permission",
        created_at="2026-08-12T11:00:00+08:00",
        execution_mode="dry_run",
    )
    query = OperatorQueryService(store, now=lambda: "2026-08-13T12:00:00+08:00")

    production = query.list_feedback(execution_mode="production")
    combined = query.list_feedback(execution_mode="all")

    assert [item["execution_mode"] for item in production] == ["production"]
    assert {item["execution_mode"] for item in combined} == {
        "production",
        "dry_run",
    }


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


def test_dashboard_snapshot_health_summary_matches_open_health_issues(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    config = _config()
    store.import_product_policy_from_config(config)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO runs(
              run_id, started_at, status, dry_run, last_heartbeat_at,
              last_tick_started_at, last_tick_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_live",
                "2026-06-22T09:58:00+08:00",
                "running",
                1,
                "2026-06-22T09:59:00+08:00",
                "2026-06-22T09:58:00+08:00",
                "running",
            ),
        )
        conn.execute(
            """
            INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_live",
                "lark",
                "warning",
                "failed",
                "Lark failed",
                "{}",
                "2026-06-22T09:57:00+08:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_live",
                "hermes",
                "warning",
                "ok",
                "Hermes recovered",
                "{}",
                "2026-06-22T09:56:00+08:00",
            ),
        )
        for minute in range(37, 56):
            conn.execute(
                """
                INSERT INTO health_checks(run_id, check_name, severity, status, message, details_json, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run_live",
                    "hermes",
                    "warning",
                    "failed",
                    "Hermes failed",
                    "{}",
                    f"2026-06-22T09:{minute:02d}:00+08:00",
                ),
            )
    query = OperatorQueryService(
        store,
        policy_import_source=config,
        now=lambda: "2026-06-22T10:00:00+08:00",
    )

    snapshot = query.dashboard_snapshot()
    health = query.health_issues()

    assert snapshot["recent_health_warnings"] == [
        {
            "check_name": "lark",
            "severity": "warning",
            "status": "failed",
            "message": "Lark failed",
            "checked_at": "2026-06-22T09:57:00+08:00",
        }
    ]
    assert snapshot["health_issue_summary"] == health["summary"]
    assert snapshot["health_issue_summary"]["open_issue_count"] == 1


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
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_audits(
              backend_provider, request_type, task_id, agent_session_id,
              input_message_ids_json, input_resource_ids_json, response_json,
              latency_ms, prompt_json, tool_permissions_profile, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hermes",
                "task_session",
                task_id,
                "session_1",
                json.dumps(["om_root"]),
                json.dumps([]),
                json.dumps({"reply": "hello", "tool_calls": []}),
                42,
                json.dumps({"messages": ["debug prompt"]}),
                "default",
                "2026-06-22T08:01:00+08:00",
            ),
        )
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
    assert detail["agent_audits"][0]["request_type"] == "task_session"
    assert detail["agent_audits"][0]["input_message_ids"] == ["om_root"]
    assert detail["agent_audits"][0]["response_summary"] == {
        "reply": "hello",
        "tool_calls": "0 item(s)",
    }
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
    resource_path = tmp_path / "resources" / "om_root.txt"
    resource_path.parent.mkdir()
    resource_path.write_text("attachment", encoding="utf-8")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO routing_audits(message_id, task_id, route, route_reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("om_root", task_id, "new_task", "new task", "2026-06-22T08:00:00+08:00"),
        )
        conn.execute(
            """
            INSERT INTO message_processing(
              message_id, task_id, stage, status, attempt_count, last_error,
              terminal_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "om_root",
                task_id,
                "resource_download",
                "processed",
                1,
                None,
                None,
                "2026-06-22T08:00:00+08:00",
                "2026-06-22T08:01:00+08:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO resources(
              message_id, file_key, resource_type, download_status, path,
              sha256, raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "om_root",
                "file_1",
                "image",
                "downloaded",
                "resources/om_root.txt",
                "0123456789abcdef",
                json.dumps({"reason": "ok", "private": "omitted"}),
                "2026-06-22T08:00:00+08:00",
                "2026-06-22T08:01:00+08:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_audits(
              backend_provider, request_type, task_id, agent_session_id,
              input_message_ids_json, input_resource_ids_json, response_json,
              error, latency_ms, prompt_json, tool_permissions_profile, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hermes",
                "router",
                None,
                None,
                json.dumps(["om_root"]),
                json.dumps([1]),
                json.dumps({"route": "new_task", "candidates": [task_id]}),
                None,
                7,
                json.dumps({"prompt": "router debug"}),
                "read_only",
                "2026-06-22T08:02:00+08:00",
            ),
        )
        for index in range(51):
            conn.execute(
                """
                INSERT INTO agent_audits(
                  backend_provider, request_type, task_id, agent_session_id,
                  input_message_ids_json, input_resource_ids_json, response_json,
                  error, latency_ms, prompt_json, tool_permissions_profile, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "hermes",
                    "router",
                    None,
                    None,
                    json.dumps([f"om_root_shadow_{index}"]),
                    json.dumps([]),
                    json.dumps({"route": "ignore"}),
                    None,
                    5,
                    json.dumps({}),
                    "read_only",
                    f"2026-06-22T08:03:{index:02d}+08:00",
                ),
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
    query = OperatorQueryService(
        store,
        base_dir=tmp_path,
        now=lambda: "2026-06-22T10:00:00+08:00",
    )

    detail = query.message_detail("om_root")

    assert detail is not None
    assert detail["message"]["message_id"] == "om_root"
    assert detail["task_ids"] == [task_id]
    assert detail["task_summaries"][0]["task_id"] == "t_1"
    assert detail["routing_audits"][0]["route"] == "new_task"
    assert detail["processing"][0]["stage"] == "resource_download"
    assert detail["processing"][0]["status"] == "processed"
    assert detail["resources"][0]["file_key"] == "file_1"
    assert detail["resources"][0]["path_exists"] is True
    assert detail["resources"][0]["raw_summary"] == {"reason": "ok"}
    assert [audit["request_type"] for audit in detail["agent_audits"]] == ["router"]
    assert detail["agent_audits"][0]["task_id"] is None
    assert detail["agent_audits"][0]["input_message_ids"] == ["om_root"]
    assert detail["agent_audits"][0]["response_summary"] == {
        "route": "new_task",
        "candidates": "1 item(s)",
    }
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


def test_policy_audit_history_summarizes_deleted_chat_policy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.import_product_policy_from_config(
        _config(
            chats={"oc_delete": ChatPolicyConfig(name="Delete me", auto_reply=True)}
        )
    )
    store.delete_chat_product_policy(
        "oc_delete",
        actor="test_operator",
        reason="remove override",
    )

    history = OperatorQueryService(store).policy_audit_history(
        scope="chat",
        policy_key="chat:oc_delete",
        limit=1,
        offset=0,
    )

    assert len(history) == 1
    assert history[0]["old_summary"]["chat_id"] == "oc_delete"
    assert history[0]["old_summary"]["auto_reply"] is True
    assert history[0]["new_summary"] == {}


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
