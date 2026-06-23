from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.config import AppConfig, ChatPolicyConfig, OwnerConfig, ReplyPolicyConfig
from feishu_shadow_agent.ingestion import IngestionService
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.processing import ApprovalService, SendComposer, TaskProcessingService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HermesCliResult, LarkCliResult, MessagePage, NormalizedMessage


class FakeHermes:
    def __init__(self):
        self.router_outputs: list[dict[str, Any] | HermesCliResult | Exception] = []
        self.session_outputs: list[dict[str, Any] | HermesCliResult | Exception] = []
        self.session_ids_seen: list[str | None] = []
        self.session_prompts: list[str] = []

    def task_router(self, prompt: str) -> HermesCliResult:
        output = self.router_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, HermesCliResult):
            return output
        return HermesCliResult(["hermes"], 0, json_data=output, session_id="router_sid")

    def task_session(self, prompt: str, *, session_id: str | None = None) -> HermesCliResult:
        self.session_ids_seen.append(session_id)
        self.session_prompts.append(prompt)
        output = self.session_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, HermesCliResult):
            return output
        output = dict(output)
        hermes_session_id = output.pop("_session_id", "sid_1")
        return HermesCliResult(["hermes"], 0, json_data=output, session_id=hermes_session_id)


class FakeFeishu:
    def __init__(self):
        self.pages: dict[str | None, MessagePage | Exception] = {}
        self.calls: list[str] = []

    def version(self) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "--version"], 0, stdout="lark-cli version 1.0.56")

    def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        return LarkCliResult(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            0,
            json_data={"identities": {"bot": {"openId": "ou_bot", "available": True, "status": "ready"}}},
        )

    def owner_message(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "im", "+messages-send"], 0, json_data={})

    def search_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def list_chat_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def list_thread_messages(self, **kwargs: Any) -> MessagePage:
        return MessagePage([])

    def list_p2p_messages(self, *, user_id: str, page_token: str | None = None, **kwargs: Any) -> MessagePage:
        self.calls.append(f"p2p:{user_id}:{page_token}")
        page = self.pages.get(page_token, MessagePage([]))
        if isinstance(page, Exception):
            raise page
        return page

    def download_resource(self, **kwargs: Any) -> LarkCliResult:
        return LarkCliResult(["lark-cli", "im", "+messages-resources-download"], 0, json_data={})


def _config(**kwargs: Any) -> AppConfig:
    return AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"), **kwargs)


def _message(
    message_id: str,
    *,
    chat_id: str = "oc_1",
    chat_type: str = "group",
    sender_id: str = "ou_ext",
    sender_name: str = "Ext",
    text: str = "hello",
    mentions: list[dict[str, str]] | None = None,
    image_key: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"text": text}
    if mentions is not None:
        content["mentions"] = mentions
    if image_key:
        content["image_key"] = image_key
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_type": "user",
        "create_time": "2026-06-22T10:00:00+08:00",
        "content": content,
    }


def _session_output(**overrides: Any) -> dict[str, Any]:
    base = {
        "task_label": "label",
        "task_state": "needs_reply",
        "answerability": "auto_reply",
        "confidence": 0.95,
        "proposed_reply": "reply text",
        "reply_target_message_id": "om_1",
        "watch_action": "keep_watching",
        "watch_extend_minutes": 120,
        "risk_level": "low",
        "safety_notes": [],
        "requires_resources": False,
    }
    return base | overrides


def _service(tmp_path: Path, *, config: AppConfig | None = None, hermes: FakeHermes | None = None) -> tuple[SQLiteStore, IngestionService, FakeHermes]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    fake_hermes = hermes or FakeHermes()
    cfg = config or _config()
    processor = TaskProcessingService(
        store=store,
        config=cfg,
        hermes_client=fake_hermes,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        hermes_retry_delays_seconds=(0.0, 0.0),
    )
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        task_processor=processor,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    return store, service, fake_hermes


def test_gate_passed_p2p_creates_pending_send_and_persists_session(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a", sender_name="Alice"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        task = conn.execute("SELECT hermes_session_id FROM tasks").fetchone()
        action = conn.execute("SELECT kind, status, payload_json FROM actions").fetchone()
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    payload = json.loads(action["payload_json"])
    assert task["hermes_session_id"] == "sid_1"
    assert action["kind"] == "send_reply"
    assert action["status"] == "pending"
    assert payload["identity"] == "user"
    assert approvals == 0


def test_group_auto_reply_disabled_downgrades_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT short_id, status, preview, payload_json FROM approvals").fetchone()
        notification = conn.execute(
            "SELECT kind, status, payload_json FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert approval["status"] == "pending"
    assert approval["preview"] == "reply text"
    assert payload["text"] == '<at user_id="ou_ext">Ext</at> reply text'
    assert notification["status"] == "pending"
    assert notify_payload["approval_id"] == approval["short_id"]
    assert notify_payload["commands"] == [
        f"/approve {approval['short_id']}",
        f"/send {notify_payload['task_id']} <final reply>",
        f"/reject {approval['short_id']}",
    ]
    assert send_count == 0

    result = store.apply_approval_command(
        message_id="om_approve",
        command=f"/approve {approval['short_id']}",
        verb="approve",
        target_id=approval["short_id"],
    )

    assert result["status"] == "applied"
    with store.connect() as conn:
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
    action_payload = json.loads(action["payload_json"])
    assert action_payload["text"] == '<at user_id="ou_ext">Ext</at> reply text'


def test_forbidden_hermes_mentions_downgrade_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(reply_target_message_id="om_1", proposed_reply='<at user_id="ou_x">X</at> hi')
    )
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT short_id, payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 0
    payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert payload["approvable"] is False
    assert payload["text"] == ""
    assert notify_payload["approval_id"] == approval["short_id"]
    assert notify_payload["commands"] == [
        f"/send {notify_payload['task_id']} <final reply>",
        f"/reject {approval['short_id']}",
    ]
    assert all(not command.startswith("/approve") for command in notify_payload["commands"])

    result = store.apply_approval_command(
        message_id="om_forbidden_approve",
        command=f"/approve {approval['short_id']}",
        verb="approve",
        target_id=approval["short_id"],
    )

    assert result["status"] == "failed"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 0


def test_explicit_group_auto_reply_false_overrides_global_default(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(
        reply_policy=ReplyPolicyConfig(default_group_auto_reply=True),
        chats={"oc_1": ChatPolicyConfig(auto_reply=False, bot_joined=True)},
    )
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert payload["reason"] == "group_auto_reply_disabled"
    assert send_count == 0


def test_unknown_group_does_not_auto_reply_even_when_global_default_enabled(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(reply_policy=ReplyPolicyConfig(default_group_auto_reply=True))
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT status, payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT kind, status FROM actions WHERE kind = 'owner_notification'").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert approval["status"] == "pending"
    assert payload["reason"] == "unknown_group_auto_reply_disabled"
    assert notification["kind"] == "owner_notification"
    assert notification["status"] == "pending"
    assert send_count == 0


def test_explicit_bot_identity_requires_bot_joined(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=False, reply_identity="bot")})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert payload["reason"] == "bot_not_joined"
    assert send_count == 0


def test_resource_dependent_bot_not_joined_creates_owner_notification(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", requires_resources=True))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=False)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}], image_key="img_1"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        resource = conn.execute("SELECT download_status FROM resources").fetchone()
        notification = conn.execute("SELECT kind, status, payload_json FROM actions").fetchone()
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    assert resource["download_status"] == "bot_not_joined"
    assert notification["kind"] == "owner_notification"
    assert "resource_needs_bot" in notification["payload_json"]
    assert approvals == 0


def test_resource_required_without_resource_records_downgrades_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", requires_resources=True))
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT status, payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT kind, status FROM actions WHERE kind = 'owner_notification'").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert approval["status"] == "pending"
    assert payload["reason"] == "resource_missing"
    assert notification["kind"] == "owner_notification"
    assert notification["status"] == "pending"
    assert send_count == 0


def test_task_router_placeholder_can_create_new_task(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append({
        "route": "new_task",
        "target_task_id": None,
        "confidence": 0.9,
        "reason": "new",
        "updated_watch_keys": ["user:ou_extra"],
    })
    hermes.session_outputs.append(_session_output(_session_id="sid_2", reply_target_message_id="om_2"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) AS c FROM hermes_audits WHERE request_type = 'router'").fetchone()["c"] == 1
        route = conn.execute(
            """
            SELECT route_reason, router_called, matched_by
            FROM routing_audits
            WHERE message_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            ("om_2",),
        ).fetchone()
        task_id = conn.execute("SELECT id FROM tasks WHERE root_message_id = ?", ("om_2",)).fetchone()["id"]
        watch_key = conn.execute(
            "SELECT key FROM task_watch_keys WHERE task_id = ? AND key = ?",
            (task_id, "user:ou_extra"),
        ).fetchone()
    assert route["route_reason"] == "new"
    assert route["router_called"] == 1
    assert route["matched_by"] == "task_router"
    assert watch_key["key"] == "user:ou_extra"


def test_task_router_invalid_updated_watch_key_downgrades_to_ambiguity(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    with store.connect() as conn:
        task_short_id = conn.execute("SELECT short_id FROM tasks WHERE root_message_id = ?", ("om_1",)).fetchone()["short_id"]
    hermes.router_outputs.append({
        "route": "attach_task",
        "target_task_id": task_short_id,
        "confidence": 0.9,
        "reason": "attach",
        "updated_watch_keys": ["user:ou_ok", "bad:ou_no"],
    })
    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        route = conn.execute(
            "SELECT route, route_reason, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        notification = conn.execute("SELECT payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
    assert route["route"] == "ambiguous"
    assert route["route_reason"] == "task_router_invalid_watch_keys"
    assert route["router_called"] == 1
    assert "bad:ou_no" in notification["payload_json"]


def test_task_router_ignore_records_audit_without_notification(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append({"route": "ignore", "target_task_id": None, "confidence": 0.9, "reason": "not actionable", "updated_watch_keys": []})
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        route = conn.execute(
            "SELECT route FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()["route"]
        notifications = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'owner_notification'").fetchone()["c"]
    assert route == "ignore"
    assert notifications == 0


def test_task_router_invalid_target_records_ambiguous_audit_and_notification(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": "t_missing",
            "confidence": 0.9,
            "reason": "bad target",
            "updated_watch_keys": [],
        }
    )
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        route = conn.execute(
            "SELECT route, route_reason, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()
    assert route["route"] == "ambiguous"
    assert route["route_reason"] == "task_router_invalid_target"
    assert route["router_called"] == 1
    assert "task_router_invalid_target" in notification["payload_json"]


@pytest.mark.parametrize("route", ["attach_task", "reopen_task", "close_task"])
def test_task_router_existing_non_candidate_target_is_invalid(tmp_path: Path, route: str) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.session_outputs.append(_session_output(_session_id="sid_stray", reply_target_message_id="om_2"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    stray = service.normalizer.normalize(
        _message("om_stray", chat_id="oc_other", sender_id="ou_stray", sender_name="Stray"),
        default_chat_type="group",
    )
    store.upsert_message(stray)
    stray_task = store.create_task_for_message(
        stray,
        watch_until="2026-06-22T12:10:00+08:00",
        task_label="stray task",
    )
    hermes.router_outputs.append(
        {
            "route": route,
            "target_task_id": stray_task.short_id,
            "confidence": 0.9,
            "reason": "wrong existing target",
            "updated_watch_keys": [],
        }
    )

    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        route_row = conn.execute(
            "SELECT route, route_reason, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        stray_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (stray_task.id,)).fetchone()["status"]
        stray_link_count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_messages WHERE task_id = ? AND message_id = ?",
            (stray_task.id, "om_2"),
        ).fetchone()["c"]
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%task_router_invalid_target%",),
        ).fetchone()
    assert route_row["route"] == "ambiguous"
    assert route_row["route_reason"] == "task_router_invalid_target"
    assert route_row["router_called"] == 1
    assert stray_status == "watching"
    assert stray_link_count == 0
    assert notification is not None


def test_task_router_can_reopen_historical_closed_recall_candidate(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_2"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.close_task_for_owner_takeover(created.task.id)

    hermes.router_outputs.append(
        {
            "route": "reopen_task",
            "target_task_id": created.task.short_id,
            "confidence": 0.9,
            "reason": "historical follow-up",
            "updated_watch_keys": ["user:ou_a"],
        }
    )

    service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        route_row = conn.execute(
            "SELECT route, route_reason, target_task_id, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        task_message = conn.execute(
            "SELECT role FROM task_messages WHERE task_id = ? AND message_id = ?",
            (created.task.id, "om_2"),
        ).fetchone()
        invalid_notification = conn.execute(
            "SELECT id FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%task_router_invalid_target%",),
        ).fetchone()
    assert route_row["route"] == "reopen_task"
    assert route_row["route_reason"] == "historical follow-up"
    assert route_row["target_task_id"] == created.task.id
    assert route_row["router_called"] == 1
    assert task["status"] == "watching"
    assert task["closed_at"] is None
    assert task_message["role"] == "follow_up"
    assert invalid_notification is None


def test_task_router_failure_records_ambiguous_audit(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append(HermesCliResult(["hermes"], 1, error="boom"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    service.process_raw_message(
        _message("om_2", sender_id="ou_b", sender_name="Bob", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        route = conn.execute(
            "SELECT route, route_reason, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
    assert route["route"] == "ambiguous"
    assert route["route_reason"] == "task_router_failed"
    assert route["router_called"] == 1


def test_task_session_followup_uses_stored_hermes_session(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_2"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert hermes.session_ids_seen == [None, "sid_1"]
    followup_prompt = json.loads(hermes.session_prompts[1])
    assert [message["message_id"] for message in followup_prompt["messages"]] == ["om_2"]
    assert followup_prompt["metadata"] == {
        "current_message_id": "om_2",
        "root_message_id": "om_1",
        "reply_target_message_ids": ["om_2", "om_1"],
    }
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 2
        audit = conn.execute(
            """
            SELECT input_message_ids_json, input_resource_ids_json
            FROM hermes_audits
            WHERE request_type = 'task_session'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert json.loads(audit["input_message_ids_json"]) == ["om_2"]
    assert json.loads(audit["input_resource_ids_json"]) == []


def test_task_session_schema_failure_does_not_persist_session_id(tmp_path: Path) -> None:
    hermes = FakeHermes()
    invalid_output = _session_output(_session_id="sid_bad", reply_target_message_id="om_1")
    invalid_output.pop("task_label")
    hermes.session_outputs.append(invalid_output)
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        task = conn.execute("SELECT hermes_session_id FROM tasks").fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        processing = conn.execute(
            "SELECT stage, status, attempt_count, terminal_reason FROM message_processing WHERE message_id = ?",
            ("om_1",),
        ).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()
    assert task["hermes_session_id"] is None
    assert approval_count == 0
    assert processing["stage"] == "task_session"
    assert processing["status"] == "processing_failed_terminal"
    assert processing["attempt_count"] == 1
    assert processing["terminal_reason"] == "hermes_schema_failed"
    assert "processing_failed" in notification["payload_json"]


def test_task_session_exception_retries_terminal_without_empty_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.extend(
        [
            RuntimeError("session exploded"),
            RuntimeError("session exploded"),
            RuntimeError("session exploded"),
        ]
    )
    store, service, _ = _service(tmp_path, hermes=hermes)
    raw = _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a")

    result = service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")

    assert result is not None
    assert hermes.session_ids_seen == [None, None, None]
    with store.connect() as conn:
        processing = conn.execute(
            "SELECT stage, status, attempt_count, terminal_reason, last_error FROM message_processing WHERE message_id = ?",
            ("om_1",),
        ).fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        notification = conn.execute(
            "SELECT id, idempotency_key, payload_json FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()
        task = conn.execute("SELECT status FROM tasks").fetchone()
    payload = json.loads(notification["payload_json"])
    assert processing["stage"] == "task_session"
    assert processing["status"] == "processing_failed_terminal"
    assert processing["attempt_count"] == 3
    assert processing["terminal_reason"] == "hermes_task_session_failed"
    assert "session exploded" in processing["last_error"]
    assert approval_count == 0
    assert payload["type"] == "processing_failed"
    assert payload["message_id"] == "om_1"
    assert payload["stage"] == "task_session"
    assert payload["dedupe_key"] == "owner-processing-failed:om_1:task_session"
    assert task["status"] == "watching"

    service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")

    assert hermes.session_ids_seen == [None, None, None]
    with store.connect() as conn:
        notification_count = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()["c"]
    assert notification_count == 1


def test_duplicate_with_routing_audit_but_no_processing_reruns_task_session(tmp_path: Path) -> None:
    cfg = _config()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    raw = _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a")
    p2_service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    first = p2_service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_1")
    assert first is not None
    assert first.decision.route == "new_task"

    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    processor = TaskProcessingService(
        store=store,
        config=cfg,
        hermes_client=hermes,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        hermes_retry_delays_seconds=(0.0, 0.0),
    )
    p3_service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        task_processor=processor,
        clock=lambda: "2026-06-22T10:11:00+08:00",
    )

    second = p3_service.process_raw_message(raw, source="p2p", default_chat_type="p2p", run_id="run_2")

    assert second is not None
    assert second.decision.route == "new_task"
    assert hermes.session_ids_seen == [None]
    with store.connect() as conn:
        action = conn.execute("SELECT kind, status FROM actions WHERE kind = 'send_reply'").fetchone()
        processing = conn.execute(
            "SELECT stage, status, attempt_count FROM message_processing WHERE message_id = ?",
            ("om_1",),
        ).fetchone()
    assert action["status"] == "pending"
    assert processing["stage"] == "task_session"
    assert processing["status"] == "processed"
    assert processing["attempt_count"] == 1


def test_empty_auto_reply_downgrades_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", proposed_reply="   "))
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT status, payload_json FROM approvals").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert approval["status"] == "pending"
    assert payload["reason"] == "empty_proposed_reply"
    assert payload["approvable"] is False
    assert payload["text"] == ""
    assert send_count == 0


def test_send_composer_mentions_reply_target_sender_once() -> None:
    composer = SendComposer(owner_open_id="ou_owner")
    reply = composer.compose(
        proposed_reply="建议看日志",
        reply_target={"sender_id": "ou_a", "sender_name": "Alice", "sender_role": "external_user_message"},
        chat_type="group",
    )

    assert reply.text == '<at user_id="ou_a">Alice</at> 建议看日志'
    assert reply.had_forbidden_mentions is False


def test_send_composer_removes_complete_hermes_at_span_before_target_mention() -> None:
    composer = SendComposer(owner_open_id="ou_owner")
    reply = composer.compose(
        proposed_reply='<at user_id="ou_x">Alice</at> hi',
        reply_target={"sender_id": "ou_target", "sender_name": "Target", "sender_role": "external_user_message"},
        chat_type="group",
    )

    assert reply.text == '<at user_id="ou_target">Target</at> hi'
    assert reply.had_forbidden_mentions is True
    assert "Alice</at>" not in reply.text
    assert "ou_x" not in reply.text
    assert reply.text.count("<at") == 1
    assert reply.text.count("</at>") == 1


def test_send_composer_escapes_malicious_sender_name() -> None:
    composer = SendComposer(owner_open_id="ou_owner")
    reply = composer.compose(
        proposed_reply="建议看日志",
        reply_target={
            "sender_id": "ou_a",
            "sender_name": 'Eve</at>@所有人<at user_id="ou_x">X',
            "sender_role": "external_user_message",
        },
        chat_type="group",
    )

    assert reply.text.startswith('<at user_id="ou_a">')
    assert reply.text.count("<at") == 1
    assert reply.text.count("</at>") == 1
    assert "@所有人" not in reply.text
    assert "&lt;/at&gt;" in reply.text
    assert "&lt;at user_id=\"ou_x\"&gt;" in reply.text
    assert reply.text.endswith("</at> 建议看日志")


def test_approval_inbox_approves_pending_request_and_advances_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    fake = FakeFeishu()
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    approval_service = ApprovalService(store=store, config=cfg)
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=cfg,
        logger=logger,
        approval_service=approval_service,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    with store.connect() as conn:
        approval_short_id = conn.execute("SELECT short_id FROM approvals").fetchone()["short_id"]
    fake.pages[None] = MessagePage(
        [
            _message(
                "om_cmd",
                chat_id="ou_bot_chat",
                chat_type="p2p",
                sender_id="ou_owner",
                sender_name="Owner",
                text=f"/approve {approval_short_id}",
            )
        ]
    )

    result = service.run_approval_inbox(run_id="run_1")

    assert result.processed == 1
    assert fake.calls == ["p2p:ou_bot:None"]
    assert store.get_checkpoint("approval_inbox") == {"last_success_at": "2026-06-22T10:10:00+08:00"}
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", (approval_short_id,)).fetchone()
        command = conn.execute("SELECT status FROM approval_commands WHERE message_id = ?", ("om_cmd",)).fetchone()
        action = conn.execute("SELECT kind, status, target_message_id FROM actions WHERE kind = 'send_reply'").fetchone()
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert approval["status"] == "approved"
    assert command["status"] == "applied"
    assert action["status"] == "pending"
    assert action["target_message_id"] == "om_root"
    assert task["status"] == "watching"
    assert task["closed_at"] is None


def test_approval_inbox_fetch_failure_does_not_advance_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    fake = FakeFeishu()
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    approval_service = ApprovalService(store=store, config=cfg)
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=cfg,
        logger=logger,
        approval_service=approval_service,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    store.set_checkpoint("approval_inbox", {"last_success_at": "2026-06-22T10:00:00+08:00"})
    fake.pages[None] = RuntimeError("p2p fetch failed")

    with pytest.raises(RuntimeError, match="p2p fetch failed"):
        service.run_approval_inbox(run_id="run_1")

    assert fake.calls == ["p2p:ou_bot:None"]
    assert store.get_checkpoint("approval_inbox") == {"last_success_at": "2026-06-22T10:00:00+08:00"}


def test_approval_inbox_processes_existing_owner_command_message(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    fake = FakeFeishu()
    logger = JSONLLogger(tmp_path / "agent.jsonl")
    approval_service = ApprovalService(store=store, config=cfg)
    service = IngestionService(
        store=store,
        feishu_client=fake,
        config=cfg,
        logger=logger,
        approval_service=approval_service,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    with store.connect() as conn:
        approval_short_id = conn.execute("SELECT short_id FROM approvals").fetchone()["short_id"]
    command_raw = _message(
        "om_existing_cmd",
        chat_id="ou_bot_chat",
        chat_type="p2p",
        sender_id="ou_owner",
        sender_name="Owner",
        text=f"/approve {approval_short_id}",
    )
    service.process_raw_message(
        command_raw,
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    fake.pages[None] = MessagePage([command_raw])

    result = service.run_approval_inbox(run_id="run_1")

    assert result.processed == 1
    with store.connect() as conn:
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            ("om_existing_cmd",),
        ).fetchone()
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", (approval_short_id,)).fetchone()
        action = conn.execute("SELECT status, target_message_id FROM actions WHERE kind = 'send_reply'").fetchone()
    assert command["status"] == "applied"
    assert approval["status"] == "approved"
    assert action["status"] == "pending"
    assert action["target_message_id"] == "om_root"


def test_approve_task_shortcut_single_pending_sets_task_watching(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )

    result = store.apply_approval_command(
        message_id="om_approve_task_shortcut",
        command=f"/approve {created.task.short_id}",
        verb="approve",
        target_id=created.task.short_id,
    )

    assert result["status"] == "applied"
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals").fetchone()
        action = conn.execute("SELECT status FROM actions WHERE kind = 'send_reply'").fetchone()
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert approval["status"] == "approved"
    assert action["status"] == "pending"
    assert task["status"] == "watching"
    assert task["closed_at"] is None


def test_failed_approval_command_rolls_back_state_changes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    store.insert_approval_for_test(short_id="a_bad", task_id=created.task.id)

    result = store.apply_approval_command(
        message_id="om_bad_cmd",
        command="/approve a_bad",
        verb="approve",
        target_id="a_bad",
    )

    assert result["status"] == "failed"
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", ("a_bad",)).fetchone()
        command = conn.execute("SELECT status FROM approval_commands WHERE message_id = ?", ("om_bad_cmd",)).fetchone()
        actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert approval["status"] == "pending"
    assert command["status"] == "failed"
    assert actions == 0


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_approval_command_with_extra_text_fails_without_consuming_approval(tmp_path: Path, verb: str) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    with store.connect() as conn:
        approval_short_id = conn.execute("SELECT short_id FROM approvals").fetchone()["short_id"]
    message = NormalizedMessage(
        message_id=f"om_{verb}_extra",
        chat_id="ou_bot_chat",
        chat_type="p2p",
        sender_id="ou_owner",
        sender_name="Owner",
        sender_type="user",
        sender_role="owner_message",
        sent_at="2026-06-22T10:11:00+08:00",
        thread_id=None,
        reply_to_message_id=None,
        text=f"/{verb} {approval_short_id} extra text",
        direct_mention=False,
        at_all=False,
    )

    result = approval_service.apply_command(message=message)

    assert result is not None
    assert result["status"] == "failed"
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals WHERE short_id = ?", (approval_short_id,)).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            (f"om_{verb}_extra",),
        ).fetchone()
    assert approval["status"] == "pending"
    assert task["status"] == "waiting_approval"
    assert action_count == 0
    assert command["status"] == "failed"


def test_approval_creation_rolls_back_when_owner_notification_fails(tmp_path: Path) -> None:
    class FailingNotificationStore(SQLiteStore):
        def _create_owner_notification_action_locked(self, *args: Any, **kwargs: Any) -> int:
            raise RuntimeError("notification failed")

    store = FailingNotificationStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service = ApprovalService(store=store, config=cfg)

    try:
        approval_service.request_send_reply(
            task=created.task,
            reply_target_message_id="om_root",
            proposed_reply="manual reply",
            reason="test",
        )
    except RuntimeError as exc:
        assert str(exc) == "notification failed"
    else:  # pragma: no cover - defensive
        raise AssertionError("owner notification failure did not abort approval creation")

    with store.connect() as conn:
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert approval_count == 0
    assert task["status"] == "watching"


def _task_with_two_pending_approvals(tmp_path: Path) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="second",
        reason="test",
    )
    return store, created.task.short_id


def test_approve_task_shortcut_multiple_pending_approvals_creates_owner_notification(tmp_path: Path) -> None:
    store, task_short_id = _task_with_two_pending_approvals(tmp_path)

    result = store.apply_approval_command(
        message_id="om_approve_conflict",
        command=f"/approve {task_short_id}",
        verb="approve",
        target_id=task_short_id,
    )

    assert result["status"] == "failed"
    assert result["result"]["pending_approval_ids"]
    with store.connect() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            ("om_approve_conflict",),
        ).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%multiple_pending_approvals%",),
        ).fetchone()
    payload = json.loads(notification["payload_json"])
    assert pending == 2
    assert send_actions == 0
    assert command["status"] == "failed"
    assert payload["reason"] == "multiple_pending_approvals"
    assert payload["task_id"] == task_short_id
    assert len(payload["pending_approval_ids"]) == 2
    assert "concrete a_" in payload["message"]


def test_reject_task_shortcut_multiple_pending_approvals_creates_owner_notification(tmp_path: Path) -> None:
    store, task_short_id = _task_with_two_pending_approvals(tmp_path)

    result = store.apply_approval_command(
        message_id="om_reject_conflict",
        command=f"/reject {task_short_id}",
        verb="reject",
        target_id=task_short_id,
    )

    assert result["status"] == "failed"
    assert result["result"]["pending_approval_ids"]
    with store.connect() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        task = conn.execute("SELECT status FROM tasks WHERE short_id = ?", (task_short_id,)).fetchone()
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            ("om_reject_conflict",),
        ).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%multiple_pending_approvals%",),
        ).fetchone()
    payload = json.loads(notification["payload_json"])
    assert pending == 2
    assert task["status"] == "waiting_approval"
    assert command["status"] == "failed"
    assert payload["reason"] == "multiple_pending_approvals"
    assert payload["task_id"] == task_short_id
    assert len(payload["pending_approval_ids"]) == 2
    assert "concrete a_" in payload["message"]


def test_approve_task_shortcut_mixed_pending_approval_kinds_creates_owner_notification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )
    store.insert_approval_for_test(short_id="a_tool", task_id=created.task.id, kind="tool_action")

    result = store.apply_approval_command(
        message_id="om_mixed_approve_conflict",
        command=f"/approve {created.task.short_id}",
        verb="approve",
        target_id=created.task.short_id,
    )

    assert result["status"] == "failed"
    with store.connect() as conn:
        approvals = conn.execute(
            "SELECT short_id, kind, status FROM approvals WHERE task_id = ? ORDER BY short_id",
            (created.task.id,),
        ).fetchall()
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%multiple_pending_approvals%",),
        ).fetchone()
    payload = json.loads(notification["payload_json"])
    assert send_actions == 0
    assert {row["kind"] for row in approvals} == {"send_reply", "tool_action"}
    assert all(row["status"] == "pending" for row in approvals)
    assert "a_tool" in payload["pending_approval_ids"]
    assert len(payload["pending_approval_ids"]) == 2


def test_send_command_multiple_pending_approvals_creates_owner_notification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="second",
        reason="test",
    )

    result = store.apply_approval_command(
        message_id="om_send_conflict",
        command=f"/send {created.task.short_id} final",
        verb="send",
        target_id=created.task.short_id,
        final_reply="final",
    )

    assert result["status"] == "failed"
    assert result["result"]["pending_approval_ids"]
    with store.connect() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%multiple_pending_approvals%",),
        ).fetchone()
    assert pending == 2
    assert send_actions == 0
    assert "multiple_pending_approvals" in notification["payload_json"]


def test_send_command_without_pending_approval_uses_task_root_message(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None

    result = store.apply_approval_command(
        message_id="om_send_root",
        command=f"/send {created.task.short_id} final root reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="final root reply",
    )

    assert result["status"] == "applied"
    with store.connect() as conn:
        approval = conn.execute("SELECT status, payload_json FROM approvals").fetchone()
        action = conn.execute("SELECT status, target_message_id, payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    approval_payload = json.loads(approval["payload_json"])
    action_payload = json.loads(action["payload_json"])
    assert approval["status"] == "approved"
    assert approval_payload["source"] == "owner_send"
    assert action["target_message_id"] == "om_root"
    assert action_payload["text"] == "final root reply"
    assert task["status"] == "watching"


def test_send_command_creates_action_for_single_pending_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )

    result = store.apply_approval_command(
        message_id="om_send",
        command=f"/send {created.task.short_id} final reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="final reply",
    )

    assert result["status"] == "applied"
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals").fetchone()
        action = conn.execute("SELECT status, payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    payload = json.loads(action["payload_json"])
    assert approval["status"] == "approved"
    assert action["status"] == "pending"
    assert payload["text"] == "final reply"
    assert payload["source"] == "owner_send"
    assert task["status"] == "watching"


def test_concrete_approve_conflict_does_not_consume_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="second",
        reason="test",
    )
    with store.connect() as conn:
        approval_ids = [
            row["short_id"]
            for row in conn.execute("SELECT short_id FROM approvals ORDER BY id").fetchall()
        ]

    first = store.apply_approval_command(
        message_id="om_approve_first",
        command=f"/approve {approval_ids[0]}",
        verb="approve",
        target_id=approval_ids[0],
    )
    second = store.apply_approval_command(
        message_id="om_approve_second",
        command=f"/approve {approval_ids[1]}",
        verb="approve",
        target_id=approval_ids[1],
    )

    assert first["status"] == "applied"
    assert second["status"] == "failed"
    assert "active send action already exists" in second["result"]["error"]
    with store.connect() as conn:
        approvals = {
            row["short_id"]: row["status"]
            for row in conn.execute("SELECT short_id, status FROM approvals ORDER BY id").fetchall()
        }
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            ("om_approve_second",),
        ).fetchone()
    assert approvals[approval_ids[0]] == "approved"
    assert approvals[approval_ids[1]] == "pending"
    assert send_actions == 1
    assert command["status"] == "failed"


def test_send_command_active_action_conflict_rolls_back_temporary_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    existing_action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "existing", "identity": "user"},
    )
    assert existing_action_id is not None

    result = store.apply_approval_command(
        message_id="om_send_conflict_active",
        command=f"/send {created.task.short_id} final reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="final reply",
    )

    assert result["status"] == "failed"
    assert "active send action already exists" in result["result"]["error"]
    with store.connect() as conn:
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        command = conn.execute(
            "SELECT status FROM approval_commands WHERE message_id = ?",
            ("om_send_conflict_active",),
        ).fetchone()
    assert approval_count == 0
    assert send_actions == 1
    assert command["status"] == "failed"


def test_send_command_active_action_conflict_keeps_waiting_approval_status(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    approval_service = ApprovalService(store=store, config=cfg)
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first",
        reason="test",
    )
    existing_action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "existing", "identity": "user"},
    )
    assert existing_action_id is not None

    result = store.apply_approval_command(
        message_id="om_send_conflict_pending",
        command=f"/send {created.task.short_id} final reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="final reply",
    )

    assert result["status"] == "failed"
    assert "active send action already exists" in result["result"]["error"]
    with store.connect() as conn:
        approval = conn.execute("SELECT status FROM approvals").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert approval["status"] == "pending"
    assert task["status"] == "waiting_approval"
    assert send_actions == 1


def test_send_command_reuses_failed_action_for_same_final_reply(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "same reply",
            "identity": "user",
            "source": "owner_send",
        },
    )
    assert action_id is not None
    store.finish_action(action_id, status="failed", result={"error_stage": "send"})

    result = store.apply_approval_command(
        message_id="om_retry_failed_send",
        command=f"/send {created.task.short_id} same reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="same reply",
    )

    assert result["status"] == "applied"
    assert result["result"]["action_id"] == action_id
    with store.connect() as conn:
        action = conn.execute(
            "SELECT status, approval_id, payload_json, result_json FROM actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        approval = conn.execute("SELECT status FROM approvals WHERE id = ?", (action["approval_id"],)).fetchone()
    payload = json.loads(action["payload_json"])
    assert action["status"] == "pending"
    assert action["result_json"] is None
    assert payload["text"] == "same reply"
    assert payload["source"] == "owner_send"
    assert approval["status"] == "approved"


def test_send_command_reuses_failed_auto_reply_action_and_idempotency_key(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "same reply",
            "identity": "user",
            "source": "auto_reply",
        },
    )
    assert action_id is not None
    with store.connect() as conn:
        before = conn.execute("SELECT idempotency_key FROM actions WHERE id = ?", (action_id,)).fetchone()
    original_key = before["idempotency_key"]
    store.finish_action(action_id, status="failed", result={"error_stage": "send"})

    result = store.apply_approval_command(
        message_id="om_retry_failed_auto_reply",
        command=f"/send {created.task.short_id} same reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="same reply",
    )

    assert result["status"] == "applied"
    assert result["result"]["action_id"] == action_id
    with store.connect() as conn:
        actions = conn.execute(
            "SELECT id, idempotency_key, status, approval_id, payload_json, result_json FROM actions WHERE kind = 'send_reply'"
        ).fetchall()
        approval = conn.execute("SELECT status FROM approvals WHERE id = ?", (actions[0]["approval_id"],)).fetchone()
    assert len(actions) == 1
    action = actions[0]
    payload = json.loads(action["payload_json"])
    assert action["id"] == action_id
    assert action["idempotency_key"] == original_key
    assert action["status"] == "pending"
    assert action["result_json"] is None
    assert payload["text"] == "same reply"
    assert payload["source"] == "owner_send"
    assert approval["status"] == "approved"


def test_send_command_sending_action_conflict_rolls_back_temporary_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={
            "reply_target_message_id": "om_root",
            "text": "same reply",
            "identity": "user",
            "source": "owner_send",
        },
    )
    assert action_id is not None
    assert store.claim_action_for_dispatch(action_id) is not None

    result = store.apply_approval_command(
        message_id="om_send_conflict_sending",
        command=f"/send {created.task.short_id} same reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="same reply",
    )

    assert result["status"] == "failed"
    assert "active send action already exists" in result["result"]["error"]
    with store.connect() as conn:
        action = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        send_actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert action["status"] == "sending"
    assert approval_count == 0
    assert send_actions == 1


def test_send_command_preserves_owner_final_reply_format(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config()
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    created = service.process_raw_message(
        _message("om_root", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    final_reply = "line 1\n    indented  line\nline 3 with  double  spaces"
    command = f"/send {created.task.short_id} {final_reply}"
    approval_service = ApprovalService(store=store, config=cfg)
    message = NormalizedMessage(
        message_id="om_multiline_send",
        chat_id="ou_bot_chat",
        chat_type="p2p",
        sender_id="ou_owner",
        sender_name="Owner",
        sender_type="user",
        sender_role="owner_message",
        sent_at="2026-06-22T10:11:00+08:00",
        thread_id=None,
        reply_to_message_id=None,
        text=command,
        direct_mention=False,
        at_all=False,
    )

    result = approval_service.apply_command(message=message)

    assert result is not None
    assert result["status"] == "applied"
    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        stored_command = conn.execute("SELECT command FROM approval_commands").fetchone()
    approval_payload = json.loads(approval["payload_json"])
    action_payload = json.loads(action["payload_json"])
    assert approval_payload["text"] == final_reply
    assert action_payload["text"] == final_reply
    assert stored_command["command"] == command
