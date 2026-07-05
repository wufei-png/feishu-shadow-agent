from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import (
    AgentBackendConfig,
    AppConfig,
    ChatPolicyConfig,
    LifecycleConfig,
    OwnerConfig,
    ReplyPostprocessConfig,
    ReplyPostprocessOwnerStyleConfig,
    ReplyPolicyConfig,
)
from feishu_shadow_agent.ingestion import IngestionService
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.paths import resolve_agent_working_dir
from feishu_shadow_agent.processing import ApprovalService, SendComposer, TaskProcessingService
from feishu_shadow_agent.routing import RoutingResult
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage, NormalizedMessage, ResourceRef


class FakeHermes:
    provider = "hermes"

    def __init__(self):
        self.router_outputs: list[dict[str, Any] | AgentRunResult | Exception] = []
        self.session_outputs: list[dict[str, Any] | AgentRunResult | Exception] = []
        self.postprocess_outputs: list[dict[str, Any] | AgentRunResult | Exception] = []
        self.refresh_outputs: list[dict[str, Any] | AgentRunResult | Exception] = []
        self.session_ids_seen: list[str | None] = []
        self.router_cwds: list[str | None] = []
        self.session_cwds: list[str | None] = []
        self.postprocess_cwds: list[str | None] = []
        self.refresh_cwds: list[str | None] = []
        self.router_prompts: list[str] = []
        self.session_prompts: list[str] = []
        self.postprocess_prompts: list[str] = []
        self.refresh_prompts: list[str] = []

    def task_router(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        self.router_cwds.append(None if cwd is None else str(cwd))
        self.router_prompts.append(prompt)
        output = self.router_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, AgentRunResult):
            return output
        return AgentRunResult(["hermes"], 0, json_data=output, session_id="router_sid")

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        self.session_cwds.append(None if cwd is None else str(cwd))
        self.session_ids_seen.append(session_id)
        self.session_prompts.append(prompt)
        output = self.session_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, AgentRunResult):
            return output
        output = dict(output)
        agent_session_id = output.pop("_session_id", "sid_1")
        return AgentRunResult(["hermes"], 0, json_data=output, session_id=agent_session_id)

    def reply_postprocess(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        self.postprocess_cwds.append(None if cwd is None else str(cwd))
        self.postprocess_prompts.append(prompt)
        output = self.postprocess_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, AgentRunResult):
            return output
        return AgentRunResult(["hermes"], 0, json_data=output, session_id="post_sid")

    def owner_style_refresh(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        self.refresh_cwds.append(None if cwd is None else str(cwd))
        self.refresh_prompts.append(prompt)
        output = self.refresh_outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, AgentRunResult):
            return output
        return AgentRunResult(["hermes"], 0, json_data=output, session_id="refresh_sid")


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


def _postprocess_config(*, profile_path: str = "owner_style.md") -> ReplyPostprocessConfig:
    return ReplyPostprocessConfig(
        enabled=True,
        owner_style=ReplyPostprocessOwnerStyleConfig(enabled=True, profile_path=profile_path),
    )


def _seed_policy(store: SQLiteStore, config: AppConfig) -> None:
    store.import_product_policy_from_config(config)


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


def _resource_message(message_id: str, *, file_key: str = "img_1") -> NormalizedMessage:
    raw = _message(message_id, mentions=[{"open_id": "ou_owner"}], image_key=file_key)
    resource = ResourceRef(
        message_id=message_id,
        file_key=file_key,
        resource_type="image",
        raw={"image_key": file_key},
    )
    return NormalizedMessage(
        message_id=message_id,
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_ext",
        sender_name="Ext",
        sender_type="user",
        sender_role="external_user_message",
        sent_at="2026-06-22T10:00:00+08:00",
        thread_id=None,
        reply_to_message_id=None,
        text="hello",
        direct_mention=True,
        at_all=False,
        mentions=["ou_owner"],
        resources=[resource],
        raw=raw,
    )


def _session_output(*, include_task_label: bool = True, **overrides: Any) -> dict[str, Any]:
    base = {
        "answerability": "auto_reply",
        "proposed_reply": "reply text",
        "reply_target_message_id": "om_1",
        "watch_action": "keep_watching",
    }
    if include_task_label:
        base["task_label"] = "label"
    return base | overrides


def _service(
    tmp_path: Path,
    *,
    config: AppConfig | None = None,
    hermes: FakeHermes | None = None,
    store: SQLiteStore | None = None,
    config_base_dir: Path | None = None,
) -> tuple[SQLiteStore, IngestionService, FakeHermes]:
    store = store or SQLiteStore(tmp_path / "agent.sqlite3")
    fake_hermes = hermes or FakeHermes()
    cfg = config or _config()
    base_dir = config_base_dir or tmp_path
    agent_working_dir = resolve_agent_working_dir(cfg.agent_backend.working_dir, base_dir)
    _seed_policy(store, cfg)
    processor = TaskProcessingService(
        store=store,
        config=cfg,
        agent_backend=fake_hermes,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        agent_working_dir=agent_working_dir,
        config_base_dir=base_dir,
        agent_retry_delays_seconds=(0.0, 0.0),
    )
    service = IngestionService(
        store=store,
        feishu_client=FakeFeishu(),
        config=cfg,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        task_processor=processor,
        config_base_dir=base_dir,
        clock=lambda: "2026-06-22T10:10:00+08:00",
    )
    return store, service, fake_hermes


def _assert_owner_notification_context(
    payload: dict[str, Any],
    *,
    message_id: str,
    text: str,
    sender_name: str,
    chat_id: str,
) -> None:
    assert payload["incoming_message"] == {"message_id": message_id, "text": text}
    assert payload["source"]["chat_id"] == chat_id
    assert payload["source"]["sender_name"] == sender_name


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
        task = conn.execute("SELECT agent_session_id FROM tasks").fetchone()
        action = conn.execute("SELECT kind, status, payload_json FROM actions").fetchone()
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    payload = json.loads(action["payload_json"])
    assert task["agent_session_id"] == "sid_1"
    assert action["kind"] == "send_reply"
    assert action["status"] == "pending"
    assert payload["identity"] == "user"
    assert approvals == 0


def test_new_task_stores_configured_agent_working_dir(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent-root"
    agent_root.mkdir()
    cfg = _config(agent_backend=AgentBackendConfig(working_dir="agent-root"))
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes, config_base_dir=tmp_path)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    expected = str(agent_root.resolve())
    with store.connect() as conn:
        task = conn.execute("SELECT agent_working_dir FROM tasks").fetchone()
    assert task["agent_working_dir"] == expected
    assert hermes.session_cwds == [expected]


def test_existing_task_followup_uses_stored_agent_working_dir_after_config_change(tmp_path: Path) -> None:
    old_root = tmp_path / "old-agent-root"
    new_root = tmp_path / "new-agent-root"
    old_root.mkdir()
    new_root.mkdir()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    cfg_old = _config(agent_backend=AgentBackendConfig(working_dir="old-agent-root"))
    store, service, _ = _service(
        tmp_path,
        config=cfg_old,
        hermes=hermes,
        store=store,
        config_base_dir=tmp_path,
    )

    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None

    cfg_new = _config(agent_backend=AgentBackendConfig(working_dir="new-agent-root"))
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": created.task.short_id,
            "reason": "same task",
        }
    )
    hermes.session_outputs.append(
        _session_output(include_task_label=False, _session_id="sid_1", reply_target_message_id="om_2")
    )
    _, service_after_change, _ = _service(
        tmp_path,
        config=cfg_new,
        hermes=hermes,
        store=store,
        config_base_dir=tmp_path,
    )

    service_after_change.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    assert hermes.router_cwds[-1] == str(new_root.resolve())
    assert hermes.session_cwds == [str(old_root.resolve()), str(old_root.resolve())]


def test_missing_stored_agent_working_dir_blocks_only_task_session(tmp_path: Path) -> None:
    old_root = tmp_path / "old-agent-root"
    current_root = tmp_path / "current-agent-root"
    old_root.mkdir()
    current_root.mkdir()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    cfg_old = _config(agent_backend=AgentBackendConfig(working_dir="old-agent-root"))
    store, service, _ = _service(
        tmp_path,
        config=cfg_old,
        hermes=hermes,
        store=store,
        config_base_dir=tmp_path,
    )
    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    old_root.rmdir()

    cfg_current = _config(agent_backend=AgentBackendConfig(working_dir="current-agent-root"))
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": created.task.short_id,
            "reason": "same task",
        }
    )
    _, service_after_missing_cwd, _ = _service(
        tmp_path,
        config=cfg_current,
        hermes=hermes,
        store=store,
        config_base_dir=tmp_path,
    )

    routed = service_after_missing_cwd.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_2",
    )

    assert routed is not None
    assert routed.decision.route == "ambiguous"
    assert hermes.router_cwds[-1] == str(current_root.resolve())
    assert hermes.session_cwds == [str(old_root.resolve())]
    with store.connect() as conn:
        route = conn.execute(
            "SELECT route, route_reason, router_called FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        processing = conn.execute(
            """
            SELECT status, terminal_reason, last_error
            FROM message_processing
            WHERE message_id = ? AND stage = 'task_session'
            """,
            ("om_2",),
        ).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        notification = conn.execute(
            "SELECT kind, status, payload_json FROM actions WHERE kind = 'owner_notification' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    payload = json.loads(notification["payload_json"])
    assert route["route"] == "attach_task"
    assert route["router_called"] == 1
    assert processing["status"] == "blocked_waiting_external"
    assert processing["terminal_reason"] == "agent_working_dir_unavailable"
    assert "does not exist" in processing["last_error"]
    assert task["status"] == "watching"
    assert notification["status"] == "pending"
    assert payload["type"] == "agent_working_dir_unavailable"
    assert payload["commands"] == [
        f"task close --task-id {created.task.short_id} --reason agent_working_dir_unavailable"
    ]


def test_group_auto_reply_disabled_downgrades_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", watch_action="close"))
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
        task = conn.execute("SELECT status, closed_at FROM tasks").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert approval["status"] == "pending"
    assert approval["preview"] == "reply text"
    assert payload["text"] == '<at user_id="ou_ext">Ext</at> reply text'
    assert notification["status"] == "pending"
    assert task["status"] == "watching"
    assert task["closed_at"] is None
    assert notify_payload["approval_id"] == approval["short_id"]
    assert notify_payload["source"] == {
        "task_label": "label",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_name": "Ext",
        "sender_id": "ou_ext",
        "sent_at": "2026-06-22T10:00:00+08:00",
    }
    assert notify_payload["incoming_message"] == {"message_id": "om_1", "text": "hello"}
    assert notify_payload["suggested_reply"] == '<at user_id="ou_ext">Ext</at> reply text'
    assert notify_payload["approvable"] is True
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


def test_reply_postprocess_success_updates_auto_reply_payload_before_composer(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", proposed_reply="raw reply"))
    hermes.postprocess_outputs.append({"status": "ok", "final_reply": "owner-like reply"})
    cfg = _config(
        reply_postprocess=_postprocess_config(),
        chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
    )
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        action = conn.execute("SELECT payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        audit = conn.execute(
            "SELECT request_type, tool_permissions_profile, prompt_json FROM agent_audits WHERE request_type = 'reply_postprocess'"
        ).fetchone()
    payload = json.loads(action["payload_json"])
    assert payload["text"] == '<at user_id="ou_ext">Ext</at> owner-like reply'
    assert payload["postprocess"]["applied"] is True
    assert payload["postprocess"]["original_reply"] == "raw reply"
    assert payload["postprocess"]["final_reply"] == "owner-like reply"
    assert payload["postprocess"]["enabled_guidance"] == ["owner_style"]
    assert hermes.postprocess_prompts
    assert "owner_style.md" in hermes.postprocess_prompts[0]
    assert audit["tool_permissions_profile"] == "read_only"
    assert audit["prompt_json"] is None


def test_reply_postprocess_success_updates_needs_owner_approval_preview(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(
            answerability="needs_owner",
            proposed_reply="raw needs owner",
            reply_target_message_id="om_1",
        )
    )
    hermes.postprocess_outputs.append({"status": "ok", "final_reply": "owner-ish needs owner"})
    cfg = _config(reply_postprocess=_postprocess_config())
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT preview, payload_json FROM approvals").fetchone()
    payload = json.loads(approval["payload_json"])
    assert approval["preview"] == "owner-ish needs owner"
    assert payload["text"] == '<at user_id="ou_ext">Ext</at> owner-ish needs owner'
    assert payload["postprocess"]["applied"] is True
    assert payload["postprocess"]["original_reply"] == "raw needs owner"


def test_no_reply_does_not_run_reply_postprocess(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(answerability="no_reply", proposed_reply="", reply_target_message_id=None)
    )
    cfg = _config(reply_postprocess=_postprocess_config())
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert hermes.postprocess_prompts == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"] == 0


def test_reply_postprocess_skips_empty_auto_reply_candidate(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", proposed_reply="   "))
    cfg = _config(reply_postprocess=_postprocess_config())
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert hermes.postprocess_prompts == []
    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(approval["payload_json"])
    assert payload["reason"] == "empty_proposed_reply"
    assert payload["approvable"] is False
    assert payload["text"] == ""
    assert "postprocess" not in payload
    assert send_count == 0


def test_reply_postprocess_skips_empty_needs_owner_candidate(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(
            answerability="needs_owner",
            proposed_reply="",
            reply_target_message_id="om_1",
        )
    )
    cfg = _config(reply_postprocess=_postprocess_config())
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert hermes.postprocess_prompts == []
    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
    payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert payload["reason"] == "needs_owner"
    assert payload["approvable"] is False
    assert payload["text"] == ""
    assert notify_payload["suggested_reply"] == ""
    assert "postprocess" not in payload


def test_reply_postprocess_missing_profile_creates_keep_watching_approval_with_original_candidate(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(reply_target_message_id="om_1", proposed_reply="raw reply", watch_action="close")
    )
    cfg = _config(
        reply_postprocess=_postprocess_config(profile_path="missing.md"),
        chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
    )
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    assert hermes.postprocess_prompts == []
    with store.connect() as conn:
        approval = conn.execute("SELECT short_id, preview, payload_json FROM approvals").fetchone()
        task = conn.execute("SELECT status, closed_at FROM tasks").fetchone()
    payload = json.loads(approval["payload_json"])
    assert approval["preview"] == "raw reply"
    assert payload["text"] == '<at user_id="ou_ext">Ext</at> raw reply'
    assert payload["keep_watching_on_reject"] is True
    assert payload["postprocess"]["status"] == "failed"
    assert payload["postprocess"]["failure_reason"] == "profile_missing"
    assert task["status"] == "watching"
    assert task["closed_at"] is None


def test_rejecting_postprocess_failure_approval_keeps_task_and_other_pending_work(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", proposed_reply="raw reply"))
    hermes.postprocess_outputs.append({"status": "needs_owner", "final_reply": ""})
    cfg = _config(reply_postprocess=_postprocess_config())
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    approval_service = ApprovalService(store=store, config=cfg)
    other_approval_id = approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_1",
        proposed_reply="other reply",
        reason="other",
    )
    other_action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_other",
        payload={"reply_target_message_id": "om_other", "text": "other pending", "identity": "user"},
        approval_id=other_approval_id,
    )
    assert other_action_id is not None
    with store.connect() as conn:
        postprocess_approval = conn.execute(
            "SELECT short_id FROM approvals WHERE payload_json LIKE ?",
            ('%"keep_watching_on_reject": true%',),
        ).fetchone()
        other_short_id = conn.execute("SELECT short_id FROM approvals WHERE id = ?", (other_approval_id,)).fetchone()["short_id"]

    rejected = store.apply_approval_command(
        message_id="om_reject_postprocess",
        command=f"/reject {postprocess_approval['short_id']}",
        verb="reject",
        target_id=postprocess_approval["short_id"],
    )

    assert rejected["status"] == "applied"
    assert rejected["result"]["kept_watching"] is True
    with store.connect() as conn:
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        approvals = {
            row["short_id"]: row["status"]
            for row in conn.execute("SELECT short_id, status FROM approvals ORDER BY id").fetchall()
        }
        other_action = conn.execute("SELECT status FROM actions WHERE id = ?", (other_action_id,)).fetchone()
    assert task["status"] == "watching"
    assert task["closed_at"] is None
    assert approvals[postprocess_approval["short_id"]] == "rejected"
    assert approvals[other_short_id] == "pending"
    assert other_action["status"] == "pending"


def test_reply_postprocess_length_guard_routes_to_owner_review(tmp_path: Path) -> None:
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1", proposed_reply="short"))
    hermes.postprocess_outputs.append({"status": "ok", "final_reply": "x" * 301})
    cfg = _config(
        reply_postprocess=_postprocess_config(),
        chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)},
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
        audit = conn.execute("SELECT error FROM agent_audits WHERE request_type = 'reply_postprocess'").fetchone()
    payload = json.loads(approval["payload_json"])
    assert send_count == 0
    assert payload["postprocess"]["failure_reason"] == "postprocess_length_growth"
    assert payload["postprocess"]["fallback"] == "original_candidate"
    assert audit["error"] == "postprocess_length_growth"


def test_needs_owner_notification_includes_context_without_suggested_reply(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(
            answerability="needs_owner",
            proposed_reply="",
            reply_target_message_id="om_1",
            watch_action="keep_watching",
        )
    )
    store, service, _ = _service(tmp_path, hermes=hermes)

    service.process_raw_message(
        _message(
            "om_1",
            text="classification service failed to start",
            mentions=[{"open_id": "ou_owner"}],
        ),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT short_id, payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
    approval_payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert approval_payload["reason"] == "needs_owner"
    assert approval_payload["approvable"] is False
    assert approval_payload["text"] == ""
    assert notify_payload["reason"] == "needs_owner"
    assert notify_payload["incoming_message"] == {
        "message_id": "om_1",
        "text": "classification service failed to start",
    }
    assert notify_payload["source"]["chat_id"] == "oc_1"
    assert notify_payload["source"]["sender_name"] == "Ext"
    assert notify_payload["suggested_reply"] == ""
    assert notify_payload["approvable"] is False
    assert notify_payload["commands"] == [
        f"/send {notify_payload['task_id']} <final reply>",
        f"/reject {approval['short_id']}",
    ]


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
        reply_policy=ReplyPolicyConfig(unknown_group_auto_reply=True),
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


def test_unknown_group_auto_reply_disabled_downgrades_to_approval(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(reply_policy=ReplyPolicyConfig(unknown_group_auto_reply=False))
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


def test_unknown_group_auto_reply_enabled_can_use_user_fallback(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(reply_policy=ReplyPolicyConfig(unknown_group_auto_reply=True))
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        action = conn.execute("SELECT kind, status, payload_json FROM actions WHERE kind = 'send_reply'").fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    payload = json.loads(action["payload_json"])
    assert action["kind"] == "send_reply"
    assert action["status"] == "pending"
    assert payload["identity"] == "user"
    assert payload["policy_source"] == "unknown_group"
    assert approval_count == 0


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
        processing = conn.execute("SELECT stage, status, terminal_reason FROM message_processing").fetchone()
        approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
    payload = json.loads(notification["payload_json"])
    assert resource["download_status"] == "bot_not_joined"
    assert notification["kind"] == "owner_notification"
    assert "resource_needs_bot" in notification["payload_json"]
    _assert_owner_notification_context(payload, message_id="om_1", text="hello", sender_name="Ext", chat_id="oc_1")
    assert processing["stage"] == "resource_download"
    assert processing["status"] == "blocked_waiting_external"
    assert processing["terminal_reason"] == "resource_needs_bot"
    assert approvals == 0
    assert hermes.session_prompts == []


@pytest.mark.parametrize(
    ("download_status", "expected_reason"),
    [
        ("skipped", "resource_download_disabled"),
        ("too_large", "resource_too_large"),
        ("quota_exceeded", "resource_quota_exceeded"),
    ],
)
def test_resource_blockers_record_blocked_waiting_external(
    tmp_path: Path,
    download_status: str,
    expected_reason: str,
) -> None:
    hermes = FakeHermes()
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    _seed_policy(store, cfg)
    processor = TaskProcessingService(
        store=store,
        config=cfg,
        agent_backend=hermes,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        agent_retry_delays_seconds=(0.0, 0.0),
    )
    message = _resource_message("om_1")
    store.upsert_message(message)
    task, decision = store.create_task_for_message_and_audit(
        message,
        watch_until="2026-06-22T12:10:00+08:00",
    )
    store.upsert_resource(
        message.resources[0],
        download_status=download_status,
        raw={"reason": expected_reason},
    )

    result = processor.process(
        message=message,
        routing=RoutingResult(decision=decision, task=task),
        source="group_at_me",
        now="2026-06-22T10:10:00+08:00",
        watch_until="2026-06-22T12:10:00+08:00",
        run_id="run_1",
    )

    assert result is not None
    assert result.status == "owner_notification_created"
    assert result.reason == expected_reason
    with store.connect() as conn:
        processing = conn.execute(
            "SELECT stage, status, terminal_reason FROM message_processing"
        ).fetchone()
        notification = conn.execute(
            "SELECT kind, payload_json FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert processing["stage"] == "resource_download"
    assert processing["status"] == "blocked_waiting_external"
    assert processing["terminal_reason"] == expected_reason
    assert notification["kind"] == "owner_notification"
    payload = json.loads(notification["payload_json"])
    assert payload["reason"] == expected_reason
    _assert_owner_notification_context(payload, message_id="om_1", text="hello", sender_name="Ext", chat_id="oc_1")
    assert send_count == 0
    assert hermes.session_prompts == []


def test_duplicate_ingest_with_blocked_resource_does_not_rerun_task_session(tmp_path: Path) -> None:
    hermes = FakeHermes()
    cfg = _config(
        chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True, resource_download=False)}
    )
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)
    raw = _message("om_1", mentions=[{"open_id": "ou_owner"}], image_key="img_1")

    first = service.process_raw_message(
        raw,
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    second = service.process_raw_message(
        raw,
        source="group_at_me",
        default_chat_type="group",
        run_id="run_2",
    )

    assert first is not None and first.decision.route == "new_task"
    assert second is not None
    assert second.decision.route == "ignore"
    assert second.decision.reason == "duplicate_message"
    with store.connect() as conn:
        processing_rows = conn.execute(
            "SELECT stage, status, terminal_reason FROM message_processing ORDER BY id"
        ).fetchall()
        notifications = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()["c"]
        resources = conn.execute("SELECT COUNT(*) AS c FROM resources").fetchone()["c"]
    assert [(row["stage"], row["status"], row["terminal_reason"]) for row in processing_rows] == [
        ("resource_download", "blocked_waiting_external", "resource_download_disabled")
    ]
    assert notifications == 1
    assert resources == 1
    assert hermes.session_prompts == []


def test_resource_download_failure_blocks_task_session_after_retries(tmp_path: Path) -> None:
    hermes = FakeHermes()
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}], image_key="img_1"),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        resource = conn.execute("SELECT download_status FROM resources").fetchone()
        notification = conn.execute("SELECT kind, status, payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
        processing = conn.execute("SELECT stage, status, attempt_count, terminal_reason FROM message_processing").fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    payload = json.loads(notification["payload_json"])
    assert resource["download_status"] == "missing_file"
    assert notification["kind"] == "owner_notification"
    assert notification["status"] == "pending"
    assert payload["reason"] == "resource_download_failed"
    assert payload["stage"] == "resource_download"
    _assert_owner_notification_context(payload, message_id="om_1", text="hello", sender_name="Ext", chat_id="oc_1")
    assert processing["stage"] == "resource_download"
    assert processing["status"] == "processing_failed_terminal"
    assert processing["attempt_count"] == 3
    assert processing["terminal_reason"] == "resource_download_failed"
    assert approval_count == 0
    assert send_count == 0
    assert hermes.session_prompts == []


def test_task_router_placeholder_can_create_new_task(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append({
        "route": "new_task",
        "target_task_id": None,
        "reason": "new",
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
        assert conn.execute("SELECT COUNT(*) AS c FROM agent_audits WHERE request_type = 'router'").fetchone()["c"] == 1
        route = conn.execute(
            """
            SELECT route_reason, router_called, matched_by
            FROM routing_audits
            WHERE message_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            ("om_2",),
        ).fetchone()
        first_task = conn.execute(
            "SELECT id, short_id FROM tasks WHERE root_message_id = ?",
            ("om_1",),
        ).fetchone()
        task_id = conn.execute("SELECT id FROM tasks WHERE root_message_id = ?", ("om_2",)).fetchone()["id"]
        watch_key = conn.execute(
            "SELECT key FROM task_watch_keys WHERE task_id = ? AND key = ?",
            (task_id, "user:ou_b"),
        ).fetchone()
    assert route["route_reason"] == "new"
    assert route["router_called"] == 1
    assert route["matched_by"] == "task_router"
    assert watch_key["key"] == "user:ou_b"
    router_prompt = json.loads(hermes.router_prompts[0])
    assert router_prompt["active_candidates"][0]["message_count"] == 1
    assert router_prompt["context_access"]["backend"] == "sqlite"
    assert router_prompt["context_access"]["read_only_uri"].endswith("agent.sqlite3?mode=ro")
    assert router_prompt["context_access"]["query_scope"] == {
        "current_message_id": "om_2",
        "active_tasks": [{"id": first_task["id"], "short_id": first_task["short_id"]}],
        "historical_tasks": [],
    }


def test_p2p_single_active_same_task_can_attach_through_task_router(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(
        _session_output(include_task_label=False, _session_id="sid_1", reply_target_message_id="om_2")
    )
    store, service, _ = _service(tmp_path, hermes=hermes)

    created = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert created is not None and created.task is not None
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": created.task.short_id,
            "reason": "same task",
        }
    )

    service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        route = conn.execute(
            "SELECT route, route_reason, router_called, matched_by FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
        task_messages = conn.execute(
            "SELECT COUNT(*) AS c FROM task_messages WHERE task_id = ?",
            (created.task.id,),
        ).fetchone()["c"]
    assert route["route"] == "attach_task"
    assert route["route_reason"] == "same task"
    assert route["router_called"] == 1
    assert route["matched_by"] == "task_router"
    assert task_messages == 2


def test_p2p_single_active_unrelated_topic_can_create_new_task_through_task_router(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.router_outputs.append({"route": "new_task", "target_task_id": None, "reason": "new topic"})
    hermes.session_outputs.append(_session_output(_session_id="sid_2", reply_target_message_id="om_2"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    second = service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    assert first is not None and first.task is not None
    assert second is not None
    with store.connect() as conn:
        tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        route = conn.execute(
            "SELECT route, route_reason, router_called, matched_by FROM routing_audits WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            ("om_2",),
        ).fetchone()
    assert tasks == 2
    assert route["route"] == "new_task"
    assert route["route_reason"] == "new topic"
    assert route["router_called"] == 1
    assert route["matched_by"] == "task_router"


def test_task_router_ignore_records_audit_without_notification(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append({"route": "ignore", "target_task_id": None, "reason": "not actionable"})
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
            "reason": "bad target",
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
    payload = json.loads(notification["payload_json"])
    assert payload["reason"] == "task_router_invalid_target"
    _assert_owner_notification_context(payload, message_id="om_2", text="hello", sender_name="Bob", chat_id="oc_1")


def test_task_router_ambiguous_notification_includes_context(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append(
        {
            "route": "ambiguous",
            "target_task_id": None,
            "reason": "needs owner to pick a task",
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
        _message("om_2", sender_id="ou_b", sender_name="Bob", text="which task is this", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )

    with store.connect() as conn:
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()
    payload = json.loads(notification["payload_json"])
    assert payload["reason"] == "task_router_ambiguous"
    _assert_owner_notification_context(
        payload,
        message_id="om_2",
        text="which task is this",
        sender_name="Bob",
        chat_id="oc_1",
    )


@pytest.mark.parametrize("route", ["attach_task", "reopen_task"])
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
            "reason": "wrong existing target",
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


def test_task_router_close_task_output_is_schema_failure(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert first is not None and first.task is not None
    hermes.router_outputs.append(
        {
            "route": "close_task",
            "target_task_id": first.task.short_id,
            "reason": "resolved",
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
        task_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (first.task.id,)).fetchone()["status"]
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%task_router_schema_failed%",),
        ).fetchone()
    assert route_row["route"] == "ambiguous"
    assert route_row["route_reason"] == "task_router_schema_failed"
    assert route_row["router_called"] == 1
    assert task_status == "watching"
    assert notification is not None


def test_task_router_new_task_with_target_is_schema_failure(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(chats={"oc_1": ChatPolicyConfig(auto_reply=True, bot_joined=True)})
    store, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", mentions=[{"open_id": "ou_owner"}]),
        source="group_at_me",
        default_chat_type="group",
        run_id="run_1",
    )
    assert first is not None and first.task is not None
    hermes.router_outputs.append(
        {
            "route": "new_task",
            "target_task_id": first.task.short_id,
            "reason": "unexpected target",
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
        task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        om_2_task = conn.execute("SELECT id FROM tasks WHERE root_message_id = ?", ("om_2",)).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%task_router_schema_failed%",),
        ).fetchone()
    assert route_row["route"] == "ambiguous"
    assert route_row["route_reason"] == "task_router_schema_failed"
    assert route_row["router_called"] == 1
    assert task_count == 1
    assert om_2_task is None
    assert notification is not None


def test_task_router_can_reopen_historical_closed_recall_candidate(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(
        _session_output(include_task_label=False, _session_id="sid_1", reply_target_message_id="om_2")
    )
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
            "reason": "historical follow-up",
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


def test_task_router_cannot_attach_historical_closed_recall_candidate(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(
        _session_output(include_task_label=False, _session_id="sid_1", reply_target_message_id="om_2")
    )
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
            "route": "attach_task",
            "target_task_id": created.task.short_id,
            "reason": "historical follow-up",
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
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%task_router_invalid_route%",),
        ).fetchone()
        send_reply_count = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply' AND target_message_id = ?",
            ("om_2",),
        ).fetchone()["c"]
    assert route_row["route"] == "ambiguous"
    assert route_row["route_reason"] == "task_router_invalid_route"
    assert route_row["target_task_id"] is None
    assert route_row["router_called"] == 1
    assert task["status"] == "human_taken_over"
    assert task["closed_at"] is not None
    assert task_message is None
    assert notification is not None
    assert send_reply_count == 0
    assert hermes.session_ids_seen == [None]


def test_task_router_failure_records_ambiguous_audit(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    hermes.router_outputs.append(AgentRunResult(["hermes"], 1, error="boom"))
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
    hermes.session_outputs.append(
        _session_output(include_task_label=False, _session_id="sid_1", reply_target_message_id="om_2")
    )
    store, service, _ = _service(tmp_path, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert first is not None and first.task is not None
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": first.task.short_id,
            "reason": "same task",
        }
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
        "message_context_mode": "incremental_current_message",
        "included_message_count": 1,
        "task_message_count": 2,
        "history_carried_by_agent_session": True,
    }
    assert "task_label" not in followup_prompt["output_schema"]["properties"]
    assert followup_prompt["context_access"]["backend"] == "sqlite"
    assert followup_prompt["context_access"]["mode"] == "live_read_only"
    assert followup_prompt["context_access"]["read_only_uri"].endswith("agent.sqlite3?mode=ro")
    assert followup_prompt["context_access"]["allowed_tables"] == [
        "tasks",
        "task_messages",
        "messages",
        "resources",
        "routing_audits",
    ]
    assert followup_prompt["context_access"]["query_scope"] == {
        "current_message_id": "om_2",
        "task": {"id": first.task.id, "short_id": first.task.short_id},
    }
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"] == 2
        audit = conn.execute(
            """
            SELECT input_message_ids_json, input_resource_ids_json, tool_permissions_profile
            FROM agent_audits
            WHERE request_type = 'task_session'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert json.loads(audit["input_message_ids_json"]) == ["om_2"]
    assert json.loads(audit["input_resource_ids_json"]) == []
    assert audit["tool_permissions_profile"] == "guarded_write"


def test_task_session_followup_approval_notification_uses_current_message_context(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(
        _session_output(
            _session_id="sid_1",
            proposed_reply="root reply",
            reply_target_message_id="om_1",
        )
    )
    hermes.session_outputs.append(
        _session_output(
            include_task_label=False,
            _session_id="sid_1",
            proposed_reply="@All root reply",
            reply_target_message_id="om_1",
        )
    )
    store, service, _ = _service(tmp_path, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a", sender_name="Alice", text="root question"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert first is not None and first.task is not None
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": first.task.short_id,
            "reason": "same task",
        }
    )

    service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a", sender_name="Alice", text="follow-up blocker"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT payload_json FROM approvals").fetchone()
        notification = conn.execute("SELECT payload_json FROM actions WHERE kind = 'owner_notification'").fetchone()
    approval_payload = json.loads(approval["payload_json"])
    notify_payload = json.loads(notification["payload_json"])
    assert approval_payload["reason"] == "forbidden_mentions"
    assert approval_payload["reply_target_message_id"] == "om_1"
    assert notify_payload["reason"] == "forbidden_mentions"
    assert notify_payload["reply_target_message_id"] == "om_1"
    _assert_owner_notification_context(
        notify_payload,
        message_id="om_2",
        text="follow-up blocker",
        sender_name="Alice",
        chat_id="ou_chat",
    )


def test_context_access_omitted_for_read_only_tool_profile(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(reply_target_message_id="om_1"))
    cfg = _config(tool_permissions="read_only")
    _, service, _ = _service(tmp_path, config=cfg, hermes=hermes)

    service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    prompt = json.loads(hermes.session_prompts[0])
    assert "context_access" not in prompt


def test_context_access_omitted_when_database_file_is_missing(tmp_path: Path) -> None:
    processor = TaskProcessingService(
        store=SQLiteStore(tmp_path / "missing.sqlite3"),
        config=_config(),
        agent_backend=FakeHermes(),
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
    )

    assert processor._base_context_access() is None


def test_task_session_followup_rejects_task_label(tmp_path: Path) -> None:
    hermes = FakeHermes()
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_1"))
    hermes.session_outputs.append(_session_output(_session_id="sid_1", reply_target_message_id="om_2"))
    store, service, _ = _service(tmp_path, hermes=hermes)

    first = service.process_raw_message(
        _message("om_1", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )
    assert first is not None and first.task is not None
    hermes.router_outputs.append(
        {
            "route": "attach_task",
            "target_task_id": first.task.short_id,
            "reason": "same task",
        }
    )
    service.process_raw_message(
        _message("om_2", chat_id="ou_chat", chat_type="p2p", sender_id="ou_a"),
        source="p2p",
        default_chat_type="p2p",
        run_id="run_1",
    )

    with store.connect() as conn:
        processing = conn.execute(
            """
            SELECT stage, status, terminal_reason
            FROM message_processing
            WHERE message_id = ? AND stage = 'task_session'
            """,
            ("om_2",),
        ).fetchone()
        send_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification' AND payload_json LIKE ?",
            ("%agent_schema_failed%",),
        ).fetchone()
    assert processing["status"] == "processing_failed_terminal"
    assert processing["terminal_reason"] == "agent_schema_failed"
    assert send_count == 1
    assert notification is not None
    payload = json.loads(notification["payload_json"])
    _assert_owner_notification_context(payload, message_id="om_2", text="hello", sender_name="Ext", chat_id="ou_chat")


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
        task = conn.execute("SELECT agent_session_id FROM tasks").fetchone()
        approval_count = conn.execute("SELECT COUNT(*) AS c FROM approvals").fetchone()["c"]
        processing = conn.execute(
            "SELECT stage, status, attempt_count, terminal_reason FROM message_processing WHERE message_id = ?",
            ("om_1",),
        ).fetchone()
        notification = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'owner_notification'",
        ).fetchone()
    assert task["agent_session_id"] is None
    assert approval_count == 0
    assert processing["stage"] == "task_session"
    assert processing["status"] == "processing_failed_terminal"
    assert processing["attempt_count"] == 1
    assert processing["terminal_reason"] == "agent_schema_failed"
    payload = json.loads(notification["payload_json"])
    assert payload["type"] == "processing_failed"
    _assert_owner_notification_context(payload, message_id="om_1", text="hello", sender_name="Ext", chat_id="ou_chat")


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
    assert processing["terminal_reason"] == "agent_task_session_failed"
    assert "session exploded" in processing["last_error"]
    assert approval_count == 0
    assert payload["type"] == "processing_failed"
    assert payload["message_id"] == "om_1"
    assert payload["stage"] == "task_session"
    assert payload["dedupe_key"] == "owner-processing-failed:om_1:task_session"
    _assert_owner_notification_context(payload, message_id="om_1", text="hello", sender_name="Ext", chat_id="ou_chat")
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
    _seed_policy(store, cfg)
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
        agent_backend=hermes,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        agent_retry_delays_seconds=(0.0, 0.0),
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
    assert task["status"] == "watching"
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


def test_approval_request_keeps_task_watching_and_sets_expiry(tmp_path: Path) -> None:
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

    ApprovalService(store=store, config=cfg).request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )

    with store.connect() as conn:
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        approval = conn.execute("SELECT status, expires_at FROM approvals").fetchone()
    assert task["status"] == "watching"
    assert task["closed_at"] is None
    assert approval["status"] == "pending"
    assert approval["expires_at"] is not None


def test_approval_timeout_null_leaves_expires_at_null(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=None))
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

    ApprovalService(store=store, config=cfg).request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )

    with store.connect() as conn:
        approval = conn.execute("SELECT expires_at FROM approvals").fetchone()
    assert approval["expires_at"] is None


def test_approval_command_expires_overdue_before_resolving_command(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=1))
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
    approval_id = ApprovalService(store=store, config=cfg).request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    with store.connect() as conn:
        approval_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()["short_id"]
        conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", approval_id))

    result = store.apply_approval_command(
        message_id="om_approve_overdue",
        command=f"/approve {approval_short_id}",
        verb="approve",
        target_id=approval_short_id,
    )

    assert result["status"] == "failed"
    assert "pending approval not found" in result["result"]["error"]
    with store.connect() as conn:
        approval = conn.execute("SELECT status, resolved_at FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        action_count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE kind = 'send_reply'").fetchone()["c"]
    assert approval["status"] == "expired"
    assert approval["resolved_at"] is not None
    assert action_count == 0


def test_dispatchable_action_reads_do_not_expire_overdue_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=1))
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
    store.insert_approval_for_test(short_id="a_overdue", task_id=created.task.id)
    with store.connect() as conn:
        approval_id = conn.execute(
            "SELECT id FROM approvals WHERE short_id = ?",
            ("a_overdue",),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE approvals SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", approval_id),
        )
    action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "manual reply", "identity": "user"},
        approval_id=approval_id,
    )
    assert action_id is not None

    pending_count = store.count_pending_actions()
    dispatchable = store.list_dispatchable_actions()

    with store.connect() as conn:
        approval = conn.execute("SELECT status, resolved_at FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        action = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert pending_count == 1
    assert [action.id for action in dispatchable] == [action_id]
    assert approval["status"] == "pending"
    assert approval["resolved_at"] is None
    assert action["status"] == "pending"


def test_approval_expiry_cancels_related_pending_send_and_keeps_task_watching(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=1))
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
    approval_id = ApprovalService(store=store, config=cfg).request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_root",
        payload={"reply_target_message_id": "om_root", "text": "manual reply", "identity": "user"},
        approval_id=approval_id,
    )
    assert action_id is not None
    with store.connect() as conn:
        conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", ("2026-06-22T09:00:00+08:00", approval_id))

    expired = store.expire_pending_approvals(now="2026-06-22T10:30:00+08:00")

    with store.connect() as conn:
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        approval = conn.execute("SELECT status, resolved_at FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        action = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert expired == 1
    assert approval["status"] == "expired"
    assert approval["resolved_at"] == "2026-06-22T10:30:00+08:00"
    assert action["status"] == "cancelled"
    assert task["status"] == "watching"
    assert task["closed_at"] is None


def test_approval_expiry_cancels_related_owner_notification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=1))
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
    approval_id = ApprovalService(store=store, config=cfg).request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="manual reply",
        reason="test",
    )
    with store.connect() as conn:
        notification = conn.execute(
            "SELECT id, approval_id, status FROM actions WHERE kind = 'owner_notification'"
        ).fetchone()
        conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", ("2026-06-22T09:00:00+08:00", approval_id))
    assert notification["approval_id"] == approval_id
    assert notification["status"] == "pending"

    expired = store.expire_pending_approvals(now="2026-06-22T10:30:00+08:00")
    dispatchable = store.list_dispatchable_actions()

    with store.connect() as conn:
        approval = conn.execute("SELECT status, resolved_at FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        notification_after = conn.execute(
            "SELECT status FROM actions WHERE id = ?",
            (notification["id"],),
        ).fetchone()
    assert expired == 1
    assert approval["status"] == "expired"
    assert approval["resolved_at"] == "2026-06-22T10:30:00+08:00"
    assert notification_after["status"] == "cancelled"
    assert notification["id"] not in {action.id for action in dispatchable}


def test_approval_expiry_keeps_pending_send_for_other_approved_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "agent.sqlite3")
    cfg = _config(lifecycle=LifecycleConfig(approval_timeout_hours=1))
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
    first_approval_id = approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first reply",
        reason="test",
    )
    second_approval_id = approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="second reply",
        reason="test",
    )
    with store.connect() as conn:
        first_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (first_approval_id,),
        ).fetchone()["short_id"]

    approved = store.apply_approval_command(
        message_id="om_approve_first_before_other_expiry",
        command=f"/approve {first_short_id}",
        verb="approve",
        target_id=first_short_id,
    )
    assert approved["status"] == "applied"
    with store.connect() as conn:
        send_action = conn.execute(
            "SELECT id, status, approval_id FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
        conn.execute(
            "UPDATE approvals SET expires_at = ? WHERE id = ?",
            ("2026-06-22T09:00:00+08:00", second_approval_id),
        )
    assert send_action["status"] == "pending"
    assert send_action["approval_id"] == first_approval_id

    expired = store.expire_pending_approvals(now="2026-06-22T10:30:00+08:00")

    with store.connect() as conn:
        approvals = {
            row["id"]: (row["status"], row["resolved_at"])
            for row in conn.execute("SELECT id, status, resolved_at FROM approvals").fetchall()
        }
        action = conn.execute("SELECT status FROM actions WHERE id = ?", (send_action["id"],)).fetchone()
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
    assert expired == 1
    assert approvals[first_approval_id][0] == "approved"
    assert approvals[second_approval_id] == ("expired", "2026-06-22T10:30:00+08:00")
    assert action["status"] == "pending"
    assert task["status"] == "watching"
    assert task["closed_at"] is None


def test_concrete_reject_closes_task_and_prevents_other_pending_approval_revival(tmp_path: Path) -> None:
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
    approval_service = ApprovalService(store=store, config=cfg)
    first_approval_id = approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="first reply",
        reason="test",
    )
    second_approval_id = approval_service.request_send_reply(
        task=created.task,
        reply_target_message_id="om_root",
        proposed_reply="second reply",
        reason="test",
    )
    pending_action_id = store.create_send_reply_action(
        task_id=created.task.id,
        target_message_id="om_followup",
        payload={"reply_target_message_id": "om_followup", "text": "pending reply", "identity": "user"},
        approval_id=second_approval_id,
    )
    assert pending_action_id is not None
    with store.connect() as conn:
        first_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (first_approval_id,),
        ).fetchone()["short_id"]
        second_short_id = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (second_approval_id,),
        ).fetchone()["short_id"]

    rejected = store.apply_approval_command(
        message_id="om_reject_first",
        command=f"/reject {first_short_id}",
        verb="reject",
        target_id=first_short_id,
    )

    assert rejected["status"] == "applied"
    with store.connect() as conn:
        task = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        approvals = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM approvals ORDER BY id").fetchall()
        }
        pending_action = conn.execute("SELECT status FROM actions WHERE id = ?", (pending_action_id,)).fetchone()
        pending_approvals = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'").fetchone()["c"]
    assert task["status"] == "closed"
    assert task["closed_at"] is not None
    assert approvals[first_approval_id] == "rejected"
    assert approvals[second_approval_id] == "expired"
    assert pending_action["status"] == "cancelled"
    assert pending_approvals == 0

    approved = store.apply_approval_command(
        message_id="om_approve_second_after_reject",
        command=f"/approve {second_short_id}",
        verb="approve",
        target_id=second_short_id,
    )
    sent = store.apply_approval_command(
        message_id="om_send_after_reject",
        command=f"/send {created.task.short_id} late reply",
        verb="send",
        target_id=created.task.short_id,
        final_reply="late reply",
    )

    assert approved["status"] == "failed"
    assert sent["status"] == "failed"
    assert "not watching" in sent["result"]["error"]
    with store.connect() as conn:
        task_after = conn.execute("SELECT status, closed_at FROM tasks WHERE id = ?", (created.task.id,)).fetchone()
        send_actions = conn.execute(
            "SELECT status FROM actions WHERE kind = 'send_reply' ORDER BY id"
        ).fetchall()
    assert task_after["status"] == "closed"
    assert task_after["closed_at"] is not None
    assert [row["status"] for row in send_actions] == ["cancelled"]


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


def _assert_pending_approval_options(payload: dict[str, Any], *, task_short_id: str) -> None:
    pending_approvals = payload["pending_approvals"]
    assert len(pending_approvals) == 2
    assert {item["preview"] for item in pending_approvals} == {"first", "second"}
    assert {item["reason"] for item in pending_approvals} == {"test"}
    assert {item["kind"] for item in pending_approvals} == {"send_reply"}
    assert {item["approval_id"] for item in pending_approvals} == set(payload["pending_approval_ids"])
    for item in pending_approvals:
        approval_id = item["approval_id"]
        assert item["approvable"] is True
        assert item["commands"] == [
            f"/approve {approval_id}",
            f"/send {task_short_id} <final reply>",
            f"/reject {approval_id}",
        ]


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
    _assert_pending_approval_options(payload, task_short_id=task_short_id)
    _assert_owner_notification_context(payload, message_id="om_root", text="hello", sender_name="Ext", chat_id="ou_chat")
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
    assert task["status"] == "watching"
    assert command["status"] == "failed"
    assert payload["reason"] == "multiple_pending_approvals"
    assert payload["task_id"] == task_short_id
    assert len(payload["pending_approval_ids"]) == 2
    _assert_pending_approval_options(payload, task_short_id=task_short_id)
    _assert_owner_notification_context(payload, message_id="om_root", text="hello", sender_name="Ext", chat_id="ou_chat")
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
    pending_approvals = {item["approval_id"]: item for item in payload["pending_approvals"]}
    assert pending_approvals["a_tool"]["kind"] == "tool_action"
    assert pending_approvals["a_tool"]["commands"] == ["/approve a_tool", "/reject a_tool"]


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
    payload = json.loads(notification["payload_json"])
    assert payload["reason"] == "multiple_pending_approvals"
    assert payload["task_id"] == created.task.short_id
    _assert_pending_approval_options(payload, task_short_id=created.task.short_id)
    _assert_owner_notification_context(payload, message_id="om_root", text="hello", sender_name="Ext", chat_id="ou_chat")


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


def test_send_command_active_action_conflict_keeps_task_watching(tmp_path: Path) -> None:
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
    assert task["status"] == "watching"
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
    (tmp_path / "owner_style.md").write_text("# style\n", encoding="utf-8")
    cfg = _config(reply_postprocess=_postprocess_config())
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
    assert "postprocess" not in approval_payload
    assert "postprocess" not in action_payload
    assert stored_command["command"] == command
