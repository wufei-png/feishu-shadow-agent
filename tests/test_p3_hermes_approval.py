from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from feishu_shadow_agent.config import AppConfig, ChatPolicyConfig, OwnerConfig
from feishu_shadow_agent.ingestion import IngestionService
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.processing import ApprovalService, SendComposer, TaskProcessingService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import HermesCliResult, LarkCliResult, MessagePage


class FakeHermes:
    def __init__(self):
        self.router_outputs: list[dict[str, Any] | HermesCliResult] = []
        self.session_outputs: list[dict[str, Any] | HermesCliResult] = []
        self.session_ids_seen: list[str | None] = []

    def task_router(self, prompt: str) -> HermesCliResult:
        output = self.router_outputs.pop(0)
        if isinstance(output, HermesCliResult):
            return output
        return HermesCliResult(["hermes"], 0, json_data=output, session_id="router_sid")

    def task_session(self, prompt: str, *, session_id: str | None = None) -> HermesCliResult:
        self.session_ids_seen.append(session_id)
        output = self.session_outputs.pop(0)
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
        approval = conn.execute("SELECT status, preview FROM approvals").fetchone()
        notification = conn.execute("SELECT kind, status FROM actions WHERE kind = 'owner_notification'").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert approval["status"] == "pending"
    assert approval["preview"] == "reply text"
    assert notification["status"] == "pending"
    assert send_count == 0


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
        assert conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 0


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


def test_task_router_placeholder_can_create_new_task(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append({"route": "new_task", "target_task_id": None, "confidence": 0.9, "reason": "new", "updated_watch_keys": []})
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
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 2


def test_send_composer_mentions_reply_target_sender_once() -> None:
    composer = SendComposer(owner_open_id="ou_owner")
    reply = composer.compose(
        proposed_reply="建议看日志",
        reply_target={"sender_id": "ou_a", "sender_name": "Alice", "sender_role": "external_user_message"},
        chat_type="group",
    )

    assert reply.text == '<at user_id="ou_a">Alice</at> 建议看日志'
    assert reply.had_forbidden_mentions is False


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
    assert approval["status"] == "approved"
    assert command["status"] == "applied"
    assert action["status"] == "pending"
    assert action["target_message_id"] == "om_root"


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


def test_send_command_requires_exactly_one_pending_approval(tmp_path: Path) -> None:
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
    with store.connect() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        actions = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert pending == 2
    assert actions == 0


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
    payload = json.loads(action["payload_json"])
    assert approval["status"] == "approved"
    assert action["status"] == "pending"
    assert payload["text"] == "final reply"
