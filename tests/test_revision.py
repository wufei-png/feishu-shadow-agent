from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Any

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.ingestion import IngestionService, MessageNormalizer
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.paths import resolve_agent_working_dir
from feishu_shadow_agent.processing import ApprovalService, TaskProcessingService
from feishu_shadow_agent.revision import assess_revision_impact
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage, NormalizedMessage


def test_revision_assessment_keeps_identical_reply_audit_only() -> None:
    result = assess_revision_impact(
        previous_reply="同一条回复",
        current_reply="同一条回复\n",
        answerability="auto_reply",
        decision_reason=None,
        current_target_message_id="om_1",
        previous_target_message_id="om_1",
    )

    assert result.impact == "none"
    assert result.reply_changed is False


def test_revision_assessment_marks_low_risk_change_for_owner_choice() -> None:
    result = assess_revision_impact(
        previous_reply="可以，明天处理。",
        current_reply="可以，后天处理。",
        answerability="auto_reply",
        decision_reason="sufficient_evidence_low_risk",
        current_target_message_id="om_1",
        previous_target_message_id="om_1",
    )

    assert result.impact == "low"
    assert "reply_changed" in result.reasons


def test_revision_assessment_escalates_high_impact_signals() -> None:
    result = assess_revision_impact(
        previous_reply="已为你开通权限。",
        current_reply="无法开通权限。",
        answerability="needs_owner",
        decision_reason="write_or_permission",
        current_target_message_id="om_1",
        previous_target_message_id="om_1",
        revision_signals=["permission_change"],
    )

    assert result.impact == "high"
    assert "permission_change" in result.reasons


def test_revision_assessment_escalates_changed_answerability_even_same_text() -> None:
    result = assess_revision_impact(
        previous_reply="同一条回复",
        current_reply="同一条回复",
        answerability="needs_owner",
        decision_reason="insufficient_evidence",
        current_target_message_id="om_1",
        previous_target_message_id="om_1",
    )

    assert result.impact == "high"
    assert "needs_owner" in result.reasons


def test_revision_assessment_keeps_uncertain_change_owner_gated() -> None:
    result = assess_revision_impact(
        previous_reply="旧回复",
        current_reply="新回复",
        answerability="no_reply",
        decision_reason="already_resolved",
        current_target_message_id="om_1",
        previous_target_message_id="om_1",
    )

    assert result.impact == "uncertain"


def _message(text: str, *, is_deleted: bool = False) -> NormalizedMessage:
    return NormalizedMessage(
        message_id="om_source",
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_ext",
        sender_name="Ext",
        sender_type="user",
        sender_role="external_user_message",
        sent_at="2026-06-22T10:00:00+08:00",
        thread_id=None,
        reply_to_message_id=None,
        text=text,
        direct_mention=False,
        at_all=False,
        is_deleted=is_deleted,
    )


def test_message_normalizer_requires_an_explicit_tombstone_marker() -> None:
    normalizer = MessageNormalizer(owner_open_id="ou_owner")

    normal = normalizer.normalize(
        {"message_id": "om_1", "content": {"text": "hello"}},
        default_chat_type="group",
    )
    deleted = normalizer.normalize(
        {
            "message_id": "om_2",
            "content": {"text": "hello", "recalled": True},
        },
        default_chat_type="group",
    )

    assert normal.is_deleted is False
    assert deleted.is_deleted is True


def test_first_seen_tombstone_does_not_store_message_text(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")

    result = store.upsert_message_with_revision(
        replace(
            _message("secret source", is_deleted=True),
            raw={"content": {"text": "secret source"}, "recalled": True},
        )
    )

    assert result.is_deleted is True
    stored = store.get_message("om_source")
    assert stored is not None
    assert stored["text"] == ""
    assert stored["is_deleted"] == 1


def test_message_upsert_tracks_revisions_and_keeps_tombstone_terminal(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    first = _message("old source")

    inserted = store.upsert_message_with_revision(first)
    duplicate = store.upsert_message_with_revision(
        replace(first, raw={"provider_poll": "again"})
    )
    edited = store.upsert_message_with_revision(replace(first, text="edited source"))
    tombstone = store.upsert_message_with_revision(
        replace(first, is_deleted=True, raw={"recalled": True})
    )
    stale_poll = store.upsert_message_with_revision(
        replace(first, text="resurfaced", raw={"provider_poll": "stale"})
    )

    assert (inserted.inserted, inserted.changed, inserted.revision) == (True, True, 1)
    assert (duplicate.inserted, duplicate.changed, duplicate.revision) == (
        False,
        False,
        1,
    )
    assert (edited.changed, edited.revision) == (True, 2)
    assert (tombstone.changed, tombstone.revision, tombstone.is_deleted) == (
        True,
        3,
        True,
    )
    assert (stale_poll.changed, stale_poll.revision, stale_poll.is_deleted) == (
        False,
        3,
        True,
    )
    stored = store.get_message("om_source")
    assert stored is not None
    assert stored["text"] == ""
    assert stored["revision"] == 3
    assert stored["is_deleted"] == 1
    assert json.loads(stored["normalized_json"])["is_deleted"] is True
    assert json.loads(stored["raw_json"]) == {"recalled": True}


def test_routing_audit_keeps_captured_message_revision(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    store.upsert_message_with_revision(replace(source, text="edited source"))

    store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )

    with store.connect() as conn:
        audit = conn.execute(
            "SELECT revision FROM routing_audits WHERE message_id = ?",
            (source.message_id,),
        ).fetchone()
    assert audit is not None
    assert audit["revision"] == 1


def test_agent_audit_keeps_explicit_message_revision(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    store.upsert_message_with_revision(replace(source, text="edited source"))

    store.record_agent_audit(
        backend_provider="test",
        request_type="router",
        task_id=None,
        agent_session_id=None,
        input_message_ids=[source.message_id],
        input_message_revisions=[1],
        input_resource_ids=[],
        response={"route": "ignore"},
    )

    with store.connect() as conn:
        audit = conn.execute(
            "SELECT input_message_revisions_json FROM agent_audits"
        ).fetchone()
    assert audit is not None
    assert json.loads(audit["input_message_revisions_json"]) == [1]


def test_owner_notification_dedupe_key_allows_new_message_revision(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    task, _ = store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )
    service = ApprovalService(
        store=store,
        config=AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner")),
    )

    first_action_id = service.notify_owner(
        task=task,
        reason="task_session_failed",
        payload={
            "message_id": source.message_id,
            "source_message_id": source.message_id,
            "source_revision": 1,
            "dedupe_key": "owner-processing-failed:om_source:task_session:1",
        },
    )
    store.upsert_message_with_revision(replace(source, text="edited source"))
    second_action_id = service.notify_owner(
        task=task,
        reason="task_session_failed",
        payload={
            "message_id": source.message_id,
            "source_message_id": source.message_id,
            "source_revision": 2,
            "dedupe_key": "owner-processing-failed:om_source:task_session:2",
        },
    )

    assert second_action_id != first_action_id
    first_action = store.get_action(first_action_id)
    second_action = store.get_action(second_action_id)
    assert first_action is not None and first_action.source_revision == 1
    assert second_action is not None and second_action.source_revision == 2


def test_old_revision_approval_and_action_cannot_cross_send_boundary(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    task, _ = store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )
    approval_id = store.create_send_reply_approval(
        task_id=task.id,
        preview="old reply",
        payload={
            "reply_target_message_id": source.message_id,
            "text": "old reply",
            "identity": "user",
            "approvable": True,
            "source_message_id": source.message_id,
            "source_revision": 1,
        },
    )
    with store.connect() as conn:
        approval = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    assert approval is not None

    edited = store.upsert_message_with_revision(replace(source, text="edited source"))
    assert edited.revision == 2
    approval_result = store.apply_approval_command(
        message_id="om_owner_approve",
        command=f"/approve {approval['short_id']}",
        verb="approve",
        target_id=approval["short_id"],
    )
    assert approval_result["status"] == "applied"
    assert approval_result["result"]["outcome"] == "stale_revision"

    action_id = store.create_send_reply_action(
        task_id=task.id,
        target_message_id=source.message_id,
        payload={
            "reply_target_message_id": source.message_id,
            "text": "old action",
            "identity": "user",
            "source_message_id": source.message_id,
            "source_revision": 1,
        },
    )
    assert action_id is not None
    invalidated = store.invalidate_stale_revision_side_effects(
        message_id=source.message_id,
        current_revision=2,
    )
    action = store.get_action(action_id)
    assert invalidated["cancelled_actions"] == 1
    assert action is not None
    assert action.status == "cancelled"
    assert store.action_revision_is_current(action) is False


def test_completed_send_is_preserved_when_revision_fence_wins(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    task, _ = store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )
    action_id = store.create_send_reply_action(
        task_id=task.id,
        target_message_id=source.message_id,
        payload={
            "reply_target_message_id": source.message_id,
            "text": "old reply",
            "identity": "user",
            "source_message_id": source.message_id,
            "source_revision": 1,
        },
    )
    assert action_id is not None
    claim = store.claim_action_for_dispatch(action_id, run_id="run_old_revision")
    assert claim is not None

    edited = store.upsert_message_with_revision(replace(source, text="new source"))
    invalidated = store.invalidate_stale_revision_side_effects(
        message_id=source.message_id,
        current_revision=edited.revision,
    )
    assert invalidated["uncertain_actions"] == 1

    store.update_dispatch_attempt(
        claim.attempt.id,
        status="readback_ok",
        sent_message_id="om_sent",
        finish=True,
    )
    finished = store.finish_claimed_action(
        action_id,
        attempt_id=claim.attempt.id,
        status="sent",
        result={"sent_message_id": "om_sent"},
    )

    assert finished is not None
    assert finished.status == "sent"
    assert finished.result["sent_message_id"] == "om_sent"
    assert finished.result["reason"] == "stale_revision"
    assert (
        store.get_latest_sent_reply_for_source(
            message_id=source.message_id,
            before_revision=edited.revision,
        )
        is not None
    )


def test_low_level_send_action_binds_latest_external_revision(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("current source")
    store.upsert_message(source)
    task, _ = store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )

    action_id = store.create_send_reply_action(
        task_id=task.id,
        target_message_id=source.message_id,
        payload={"text": "reply", "identity": "user"},
    )

    assert action_id is not None
    action = store.get_action(action_id)
    assert action is not None
    assert action.source_message_id == source.message_id
    assert action.source_revision == 1


class _SessionBackend:
    provider = "hermes"

    def __init__(self) -> None:
        self.session_outputs: list[dict[str, Any]] = []

    def task_router(self, prompt: str, **kwargs: object) -> AgentRunResult:
        raise AssertionError("p2p revision tests must not call the task router")

    def task_session(self, prompt: str, **kwargs: object) -> AgentRunResult:
        output = dict(self.session_outputs.pop(0))
        session_id = output.pop("_session_id", "sid_1")
        return AgentRunResult(["hermes"], 0, json_data=output, session_id=session_id)

    def structured_task_session(self, prompt: str, **kwargs: object) -> AgentRunResult:
        return self.task_session(prompt)


def _raw_message(message_id: str, text: str, **extra: Any) -> dict[str, Any]:
    content: dict[str, Any] = {"text": text, **extra}
    return {
        "message_id": message_id,
        "chat_id": "ou_chat",
        "chat_type": "p2p",
        "sender_id": "ou_a",
        "sender_name": "Alice",
        "sender_type": "user",
        "create_time": "2026-06-22T10:00:00+08:00",
        "content": content,
    }


def _session_output(
    *, include_task_label: bool = True, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "answerability": "auto_reply",
        "decision_reason": None,
        "proposed_reply": "reply text",
        "reply_target_message_id": "om_source",
        "watch_action": "keep_watching",
    }
    if include_task_label:
        payload["task_label"] = "label"
    return payload | overrides


def _processing_service(
    tmp_path: Path, backend: _SessionBackend
) -> tuple[SQLiteStore, IngestionService]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"))
    store.import_product_policy_from_config(config)
    processor = TaskProcessingService(
        store=store,
        config=config,
        agent_backend=backend,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        agent_working_dir=resolve_agent_working_dir(
            config.agent_backend.working_dir, tmp_path
        ),
        config_base_dir=tmp_path,
        agent_retry_delays_seconds=(0.0, 0.0),
    )
    service = IngestionService(
        store=store,
        feishu_client=_UnusedFeishu(),
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        task_processor=processor,
        config_base_dir=tmp_path,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    return store, service


class _UnusedFeishu:
    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(["auth"], 0)

    def search_messages(self, **kwargs: object) -> MessagePage:
        return MessagePage(items=[])

    def list_chat_messages(self, **kwargs: object) -> MessagePage:
        return MessagePage(items=[])

    def list_p2p_messages(self, **kwargs: object) -> MessagePage:
        return MessagePage(items=[])

    def list_thread_messages(self, **kwargs: object) -> MessagePage:
        return MessagePage(items=[])

    def download_resource(self, **kwargs: object) -> LarkCliResult:
        return LarkCliResult(["download"], 1)


def _mark_pending_send_replies_sent(store: SQLiteStore) -> None:
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE actions
            SET status = 'sent',
                result_json = '{"sent_message_id": "om_sent"}'
            WHERE kind = 'send_reply' AND status = 'pending'
            """
        )


def _owner_notifications(store: SQLiteStore) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT payload_json FROM actions
            WHERE kind = 'owner_notification'
            ORDER BY id
            """
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def test_duplicate_tombstone_poll_does_not_repeat_routing_audit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    config = AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"))
    service = IngestionService(
        store=store,
        feishu_client=_UnusedFeishu(),
        config=config,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    first = service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert first is not None
    tombstone = _raw_message("om_source", "hello", recalled=True)
    service.process_raw_message(
        tombstone, source="p2p", default_chat_type="p2p", run_id="run_2"
    )
    service.process_raw_message(
        tombstone, source="p2p", default_chat_type="p2p", run_id="run_3"
    )

    with store.connect() as conn:
        audits = conn.execute(
            """
            SELECT route, route_reason FROM routing_audits
            WHERE message_id = 'om_source' AND route_reason = 'message_tombstone'
            """
        ).fetchall()
    assert len(audits) == 1


def test_tombstone_after_sent_reply_notifies_owner(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)

    result = service.process_raw_message(
        _raw_message("om_source", "hello", recalled=True),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    assert result is not None
    assert result.decision.reason == "message_tombstone"
    notifications = _owner_notifications(store)
    assert len(notifications) == 1
    assert notifications[0]["reason"] == "source_recalled_after_send"
    assert notifications[0]["previous_reply"] == "reply text"
    assert "/send" in notifications[0]["commands"][0]


def test_owner_send_after_source_recall_is_not_revision_fenced(
    tmp_path: Path,
) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "hello", recalled=True),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )
    task_ids = store.find_task_ids_for_message("om_source")
    task = store.get_task_by_id(task_ids[-1])

    result = store.apply_approval_command(
        message_id="om_owner_send_after_recall",
        command=f"/send {task.short_id} correction after recall",
        verb="send",
        target_id=task.short_id,
        final_reply="correction after recall",
    )

    assert result["status"] == "applied"
    action = store.get_action(result["result"]["action_id"])
    assert action is not None
    assert action.status == "pending"
    assert action.source_message_id is None
    assert store.action_revision_is_current(action) is True


def test_revision_none_does_not_auto_reply_or_notify(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(_session_output(include_task_label=False))
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "hello edited"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    with store.connect() as conn:
        send_replies = conn.execute(
            "SELECT status FROM actions WHERE kind = 'send_reply'"
        ).fetchall()
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    assert [row["status"] for row in send_replies] == ["sent"]
    assert approvals == 0
    assert _owner_notifications(store) == []


def test_revision_low_impact_notifies_with_send_command(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(
        _session_output(
            include_task_label=False,
            proposed_reply="updated reply",
            _session_id="sid_2",
        )
    )
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "hello edited"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    notifications = _owner_notifications(store)
    assert len(notifications) == 1
    assert notifications[0]["reason"] == "revision_low_impact_correction_review"
    assert notifications[0]["requires_owner_approval"] is False
    assert notifications[0]["suggested_reply"] == "updated reply"
    assert notifications[0]["commands"][0].startswith("/send ")
    with store.connect() as conn:
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    assert approvals == 0


def test_revision_high_impact_creates_approval(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(
        _session_output(
            include_task_label=False,
            proposed_reply="cannot grant that",
            answerability="needs_owner",
            decision_reason="write_or_permission",
            _session_id="sid_2",
        )
    )
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "please grant access"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    with store.connect() as conn:
        approval_row = conn.execute("SELECT payload_json FROM approvals").fetchone()
    assert approval_row is not None
    payload = json.loads(approval_row["payload_json"])
    assert payload["reason"] == "revision_correction_required"
    assert payload["requires_owner_approval"] is True


def test_revision_identical_high_impact_notifies_instead_of_sending(
    tmp_path: Path,
) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(
        _session_output(
            include_task_label=False,
            answerability="needs_owner",
            decision_reason="insufficient_evidence",
            _session_id="sid_2",
        )
    )
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "hello edited"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    notifications = _owner_notifications(store)
    assert len(notifications) == 1
    assert notifications[0]["reason"] == "revision_identical_text_review"
    with store.connect() as conn:
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        pending_sends = conn.execute(
            """
            SELECT COUNT(*) AS c FROM actions
            WHERE kind = 'send_reply' AND status = 'pending'
            """
        ).fetchone()["c"]
    assert approvals == 0
    assert pending_sends == 0


def test_revision_no_reply_notifies_owner(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(
        _session_output(
            include_task_label=False,
            answerability="no_reply",
            decision_reason="no_response_needed",
            proposed_reply="",
            reply_target_message_id=None,
            _session_id="sid_2",
        )
    )
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    _mark_pending_send_replies_sent(store)
    service.process_raw_message(
        _raw_message("om_source", "never mind"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    notifications = _owner_notifications(store)
    assert len(notifications) == 1
    assert notifications[0]["reason"] == "revision_no_reply_review"
    assert notifications[0]["requires_owner_approval"] is False


def test_owner_send_identical_text_on_new_revision_is_allowed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    task, _ = store.create_task_for_message_and_audit(
        source,
        watch_until="2026-06-22T12:00:00+08:00",
    )
    first_id = store.create_send_reply_action(
        task_id=task.id,
        target_message_id=source.message_id,
        payload={
            "reply_target_message_id": source.message_id,
            "text": "same reply",
            "identity": "user",
            "source_message_id": source.message_id,
            "source_revision": 1,
        },
    )
    assert first_id is not None
    _mark_pending_send_replies_sent(store)
    store.upsert_message_with_revision(replace(source, text="edited source"))

    result = store.apply_approval_command(
        message_id="om_owner_send",
        command=f"/send {task.short_id} same reply",
        verb="send",
        target_id=task.short_id,
        final_reply="same reply",
    )

    assert result["status"] == "applied"
    second_id = result["result"]["action_id"]
    assert second_id != first_id
    action = store.get_action(second_id)
    assert action is not None
    assert action.status == "pending"
    assert action.source_revision == 2
    assert "identical_correction_text" in action.payload["warnings"]


def test_in_flight_send_counts_as_previous_reply(tmp_path: Path) -> None:
    backend = _SessionBackend()
    backend.session_outputs.append(_session_output())
    backend.session_outputs.append(_session_output(include_task_label=False))
    store, service = _processing_service(tmp_path, backend)
    service.process_raw_message(
        _raw_message("om_source", "hello"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT id FROM actions WHERE kind = 'send_reply' AND status = 'pending'"
        ).fetchone()
    assert row is not None
    assert (
        store.claim_action_for_dispatch(int(row["id"]), run_id="run_send") is not None
    )

    service.process_raw_message(
        _raw_message("om_source", "hello edited"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    with store.connect() as conn:
        pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM actions
            WHERE kind = 'send_reply' AND status = 'pending'
            """
        ).fetchone()["c"]
        fenced = conn.execute(
            """
            SELECT status FROM actions
            WHERE kind = 'send_reply' AND id = ?
            """,
            (int(row["id"]),),
        ).fetchone()
    assert pending == 0
    assert fenced is not None
    assert fenced["status"] == "failed_needs_review"
    assert _owner_notifications(store) == []


def test_concurrent_stale_poll_cannot_revive_tombstone(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("secret source")
    store.upsert_message(source)
    started = Event()
    errors: list[BaseException] = []

    def write_tombstone() -> None:
        started.wait()
        try:
            store.upsert_message_with_revision(
                replace(source, is_deleted=True, raw={"recalled": True})
            )
        except sqlite3.Error as exc:  # pragma: no cover - thread capture
            errors.append(exc)

    def stale_poll() -> None:
        started.wait()
        try:
            store.upsert_message_with_revision(source)
        except sqlite3.Error as exc:  # pragma: no cover - thread capture
            errors.append(exc)

    tombstone_thread = Thread(target=write_tombstone)
    stale_thread = Thread(target=stale_poll)
    tombstone_thread.start()
    stale_thread.start()
    started.set()
    tombstone_thread.join(timeout=2)
    stale_thread.join(timeout=2)

    assert not errors
    stored = store.get_message(source.message_id)
    assert stored is not None
    assert stored["is_deleted"] == 1
    assert stored["text"] == ""


def test_concurrent_stale_poll_cannot_roll_back_edit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    source = _message("old source")
    store.upsert_message(source)
    started = Event()
    errors: list[BaseException] = []

    def edit() -> None:
        started.wait()
        try:
            store.upsert_message_with_revision(replace(source, text="new source"))
        except sqlite3.Error as exc:  # pragma: no cover - thread capture
            errors.append(exc)

    def stale_poll() -> None:
        started.wait()
        try:
            store.upsert_message_with_revision(source)
        except sqlite3.Error as exc:  # pragma: no cover - thread capture
            errors.append(exc)

    edit_thread = Thread(target=edit)
    stale_thread = Thread(target=stale_poll)
    edit_thread.start()
    stale_thread.start()
    started.set()
    edit_thread.join(timeout=2)
    stale_thread.join(timeout=2)

    assert not errors
    stored = store.get_message(source.message_id)
    assert stored is not None
    assert stored["is_deleted"] == 0
    if stored["revision"] == 1:
        assert stored["text"] == "old source"
    else:
        assert stored["revision"] == 2
        assert stored["text"] == "new source"
