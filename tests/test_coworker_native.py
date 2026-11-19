from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.codex_app_server import AppServerError, TurnNotActive
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    CoworkerConfig,
    OwnerConfig,
)
from feishu_shadow_agent.coworker_native import (
    NativeCoworkerService,
    NativeCoworkerWorker,
    NativeMainOutput,
    NativeReplyOutput,
    NativeWatchOutput,
    SteerDecision,
)
from feishu_shadow_agent.dispatcher import Dispatcher, DispatchSummary
from feishu_shadow_agent.feishu.client import FeishuClient
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.message_normalization import (
    MessageNormalizer,
    message_content_fingerprint,
)
from feishu_shadow_agent.operator_commands import OperatorCommandService
from feishu_shadow_agent.retention import RetentionService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


class FakeFeishu:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.topics: dict[str, list[dict[str, Any]]] = {}
        self.group_messages: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.fail_reply = False
        self.fail_actual = False
        self.omit_thread = False
        self.receipt_in_main = False
        self.sent = 0
        self.on_get: Callable[[str], None] | None = None
        self.on_preview: Callable[[], None] | None = None
        self.on_actual: Callable[[], None] | None = None

    def get_messages(self, *, as_identity: str, message_ids: list[str]) -> MessagePage:
        assert as_identity == "bot"
        if self.on_get is not None:
            self.on_get(message_ids[0])
        return MessagePage(
            items=[
                self.messages[value] for value in message_ids if value in self.messages
            ]
        )

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_token: str | None,
        page_size: int,
        as_identity: str,
        order: str,
    ) -> MessagePage:
        assert as_identity == "bot" and page_size == 50
        return MessagePage(items=self.topics.get(thread_id, []))

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_token: str | None,
        page_size: int,
        as_identity: str,
        order: str,
    ) -> MessagePage:
        assert chat_id == "group" and as_identity == "bot" and order == "desc"
        return MessagePage(items=self.group_messages)

    def reply_message(self, **kwargs: Any) -> LarkCliResult:
        self.replies.append(kwargs)
        if kwargs["dry_run"] and self.on_preview is not None:
            self.on_preview()
        if not kwargs["dry_run"]:
            self.sent += 1
            sent_id = f"sent-{self.sent}"
            source = self.messages[kwargs["message_id"]]
            self.messages[sent_id] = {
                "message_id": sent_id,
                "chat_id": source["chat_id"],
                "thread_id": "opened-thread",
                "thread_message_position": 1,
                "content": kwargs["text"],
                "msg_type": "text",
            }
            if self.omit_thread:
                self.messages[sent_id].pop("thread_id")
            if self.receipt_in_main:
                self.messages[sent_id].pop("thread_message_position")
                self.messages[sent_id]["message_position"] = 1
            if self.on_actual is not None:
                self.on_actual()
        return LarkCliResult(
            argv=[],
            exit_code=1
            if self.fail_reply or (self.fail_actual and not kwargs["dry_run"])
            else 0,
            json_data={"data": {"message_id": f"sent-{self.sent}"}},
        )


class FakeServer:
    def __init__(self) -> None:
        self.sessions: list[str | None] = []
        self.prompts: list[str] = []
        self.output_models: list[object] = []
        self.private: list[bool] = []
        self.outputs: list[dict[str, Any]] = []
        self.decisions: list[str] = []
        self.classification_prompts: list[str] = []
        self.released: list[str] = []
        self.reloaded: list[tuple[str, bool, str]] = []

    def release_thread(self, thread_id: str) -> None:
        self.released.append(thread_id)

    def reload_thread(
        self, thread_id: str, *, private: bool, cwd: str, dry_run_safety: bool = False
    ) -> None:
        self.reloaded.append((thread_id, private, cwd))

    def run_turn(
        self,
        prompt: str,
        *,
        session_id: str | None,
        cwd: str,
        output_model: object,
        on_thread: Any,
        instructions: str = "",
        private: bool = False,
        unload_delay_seconds: int | None = None,
        dry_run_safety: bool = False,
        turn_timeout_seconds: float | None = None,
        ephemeral: bool = False,
    ) -> AgentRunResult:
        if output_model is SteerDecision:
            assert session_id is None and private and dry_run_safety and ephemeral
            assert turn_timeout_seconds is not None and 0 < turn_timeout_seconds <= 30
            self.classification_prompts.append(prompt)
            decision = self.decisions.pop(0) if self.decisions else "current"
            return AgentRunResult(
                argv=[], exit_code=0, json_data={"decision": decision}
            )
        self.sessions.append(session_id)
        self.output_models.append(output_model)
        self.prompts.append(f"{instructions}\n\n{prompt}" if instructions else prompt)
        self.private.append(private)
        result_id = session_id or f"session-{len(self.sessions)}"
        on_thread(result_id)
        return AgentRunResult(
            argv=[],
            exit_code=0,
            session_id=result_id,
            json_data=self.outputs.pop(0)
            if self.outputs
            else {"action": "reply", "text": "answer"},
        )


class BlockingNativeServer(FakeServer):
    def __init__(self, *, fail_steer: bool = False, approval: bool = False) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail_steer = fail_steer
        self.approval = approval
        self.steers: list[str] = []

    def run_turn(
        self,
        prompt: str,
        *,
        session_id: str | None,
        cwd: str,
        output_model: object,
        on_thread: Any,
        instructions: str = "",
        private: bool = False,
        unload_delay_seconds: int | None = None,
        dry_run_safety: bool = False,
        turn_timeout_seconds: float | None = None,
        ephemeral: bool = False,
    ) -> AgentRunResult:
        if output_model is SteerDecision:
            return super().run_turn(
                prompt,
                session_id=session_id,
                cwd=cwd,
                output_model=output_model,
                on_thread=on_thread,
                instructions=instructions,
                private=private,
                unload_delay_seconds=unload_delay_seconds,
                dry_run_safety=dry_run_safety,
                turn_timeout_seconds=turn_timeout_seconds,
                ephemeral=ephemeral,
            )
        self.prompts.append(f"{instructions}\n\n{prompt}" if instructions else prompt)
        thread_id = session_id or "active-session"
        on_thread(thread_id)
        self.started.set()
        assert self.release.wait(timeout=5)
        return AgentRunResult(
            argv=[],
            exit_code=0,
            session_id=thread_id,
            json_data={
                "action": "reply",
                "text": "answer",
                "approval_required": self.approval,
            },
        )

    def active_turn_id(self, _thread_id: str) -> str | None:
        return (
            "active-turn"
            if self.started.is_set() and not self.release.is_set()
            else None
        )

    def steer(
        self, _thread_id: str, prompt: str, *, expected_turn_id: str | None = None
    ) -> None:
        assert expected_turn_id == "active-turn"
        self.steers.append(prompt)
        if self.fail_steer:
            raise OSError("steer outcome unknown")


class FakeNativeCoworkerService(NativeCoworkerService):
    def _classify_followup(
        self,
        policy: CoworkerConfig,
        prompt: str,
        cwd: str,
        *,
        fallback_prompt: Callable[[], str] | None = None,
    ) -> Literal["current", "unrelated", "uncertain"]:
        assert self.app_server is not None
        while True:
            result = self.app_server.run_turn(
                prompt,
                session_id=None,
                cwd=cwd,
                output_model=SteerDecision,
                on_thread=lambda _thread: None,
                private=True,
                unload_delay_seconds=1,
                dry_run_safety=True,
                turn_timeout_seconds=30,
                ephemeral=True,
            )
            if not result.ok:
                return "uncertain"
            decision = SteerDecision.model_validate(result.json_data).decision
            if decision != "uncertain" or fallback_prompt is None:
                return decision
            prompt = fallback_prompt()
            fallback_prompt = None


def _service(
    tmp_path: Path, *, dry_run: bool = True
) -> tuple[NativeCoworkerService, FakeFeishu, FakeServer]:
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(enabled=True, chat_id="group"),  # type: ignore[arg-type]
        chats={
            "group": ChatPolicyConfig(
                auto_reply=True, bot_joined=True, reply_identity="bot"
            )
        },
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config.agent_backend.working_dir = str(workspace)
    store = SQLiteStore(tmp_path / "store.sqlite3")
    store.import_product_policy_from_config(config)
    feishu, server = FakeFeishu(), FakeServer()
    service = FakeNativeCoworkerService(
        store=store,
        config=config,
        feishu=feishu,
        logger=JSONLLogger(tmp_path / "log.jsonl"),
        base_dir=tmp_path,
        app_id="app",
        dry_run=dry_run,
        chat_layout="group",
    )
    service.app_server = server  # type: ignore[assignment]
    return service, feishu, server


def _receive(
    service: NativeCoworkerService,
    feishu: FakeFeishu,
    message_id: str,
    *,
    mention: bool = True,
    parent: str | None = None,
    location: str = "main",
    thread: str = "thread",
    root: str = "root",
    sender: str = "person",
    text: str | None = None,
    chat_id: str = "group",
) -> None:
    source: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "thread_id": thread,
        "content": text or f"request {message_id}",
        "msg_type": "text",
        "message_position" if location == "main" else "thread_message_position": 1,
    }
    if mention:
        source["mentions"] = [{"id": "bot", "key": "@_user_1"}]
    feishu.messages[message_id] = source
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": "group",
        "thread_id": thread,
        "root_id": root,
        "parent_id": parent,
        "message_type": "text",
        "content": json.dumps({"text": source["content"]}),
        "mentions": [{"id": "bot"}] if mention else [],
    }
    service.receive(
        {
            "header": {"app_id": "app", "event_id": message_id},
            "event": {
                "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                "message": message,
            },
        },
        "bot",
    )


def _private(
    service: NativeCoworkerService,
    feishu: FakeFeishu,
    message_id: str,
    *,
    parent: str | None = None,
    sender: str = "owner",
    kind: str = "text",
    text: str | None = None,
) -> None:
    content = text or f"forwarded content {message_id}"
    feishu.messages[message_id] = {
        "message_id": message_id,
        "chat_id": "private-chat",
        "content": content,
        "msg_type": kind,
    }
    service.receive(
        {
            "header": {"app_id": "app", "event_id": message_id},
            "event": {
                "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                "message": {
                    "message_id": message_id,
                    "chat_id": "private-chat",
                    "chat_type": "p2p",
                    "parent_id": parent,
                    "message_type": kind,
                    "content": json.dumps({"text": content}),
                },
            },
        },
        "bot",
    )


def _apply_manual_reply(service: NativeCoworkerService) -> None:
    with service.store.connect() as conn:
        approval = conn.execute(
            "SELECT short_id FROM approvals WHERE status='pending'"
        ).fetchone()
    assert approval is not None
    result = OperatorCommandService(service.store).send(
        approval["short_id"], "approved answer", actor="owner"
    )
    assert result.status == "applied", result.as_dict()


def _dispatch_manual_reply(
    service: NativeCoworkerService, feishu: FakeFeishu
) -> DispatchSummary:
    _apply_manual_reply(service)

    def confirm_receipt() -> None:
        target = feishu.replies[-1]["message_id"]
        receipt = feishu.messages[f"sent-{feishu.sent}"]
        receipt["parent_id"] = target
        if feishu.messages[target]["chat_id"] == "private-chat":
            receipt.pop("thread_id")
            receipt.pop("thread_message_position")

    feishu.on_actual = confirm_receipt
    dispatcher = Dispatcher(
        store=service.store,
        feishu_client=cast(FeishuClient, feishu),
        config=service.config,
        logger=service.logger,
    )
    return dispatcher.dispatch(
        run_id="test",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
        include_owner_notifications=False,
        scope="coworker",
    )


def test_new_group_worker_leaves_old_group_backlog_and_deliveries_untouched(
    tmp_path: Path,
) -> None:
    before, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append(
        {"action": "reply", "text": "draft", "approval_required": True}
    )
    _receive(before, feishu, "old-approved", location="thread")
    assert before.process_next()
    _apply_manual_reply(before)
    _receive(before, feishu, "old-pending", location="thread")
    assert before.process_next()
    _receive(before, feishu, "old-queued", location="thread")
    before.store.upsert_chat_product_policy(
        {
            "chat_id": "new-group",
            "auto_reply": True,
            "bot_joined": True,
            "reply_identity": "bot",
        },
        actor="test",
    )
    before.store.update_coworker_policy({"chat_id": "new-group"}, actor="test")
    after = FakeNativeCoworkerService(
        store=before.store,
        config=before.config,
        feishu=feishu,
        logger=before.logger,
        base_dir=tmp_path,
        app_id="app",
        dry_run=False,
        chat_layout="group",
    )
    after.app_server = server  # type: ignore[assignment]
    _receive(after, feishu, "new-request", chat_id="new-group", location="thread")
    _private(after, feishu, "private-request")
    for _ in range(4):
        assert after.process_next()
    assert not after.process_next()
    dispatch = Dispatcher(
        store=after.store,
        feishu_client=cast(FeishuClient, feishu),
        config=after.config,
        logger=after.logger,
    ).dispatch(
        run_id="test",
        allow_send_reply_actual=True,
        allow_owner_notification_actual=False,
        include_owner_notifications=False,
        scope="coworker",
        coworker_chat_id="new-group",
    )
    assert dispatch.processed == 0
    assert {r["message_id"] for r in feishu.replies} == {
        "new-request",
        "private-request",
    }
    with after.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id='old-queued'"
            ).fetchone()[0]
            == "queued"
        )
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries WHERE source_message_id='old-pending'"
            ).fetchone()[0]
            == "pending"
        )
        assert (
            conn.execute(
                "SELECT status FROM actions WHERE kind='send_reply' AND source_message_id='old-approved'"
            ).fetchone()[0]
            == "pending"
        )


def test_main_and_topic_sessions_and_dry_run_receipts(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "main-1")
    assert service.process_next() and service.process_next()
    assert feishu.replies[-1]["dry_run"] is True
    assert feishu.replies[-1]["reply_in_thread"] is False
    _receive(service, feishu, "main-2")
    assert service.process_next() and service.process_next()
    _receive(service, feishu, "topic", location="thread")
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, "session-1", None]
    assert server.output_models == [
        NativeMainOutput,
        NativeMainOutput,
        NativeReplyOutput,
    ]
    assert all("子 Agent" not in prompt for prompt in server.prompts)
    assert all("本轮为只读预演" in prompt for prompt in server.prompts)
    assert all("写入本群专用记忆" not in prompt for prompt in server.prompts)
    assert feishu.replies[-1]["reply_in_thread"] is True
    with service.store.connect() as conn:
        assert [
            row[0]
            for row in conn.execute("SELECT status FROM coworker_native_deliveries")
        ] == ["previewed", "previewed", "previewed"]


@pytest.mark.parametrize(
    ("snapshot_mentions", "starts_agent"),
    [
        ([{"id": "app", "key": "@_user_1"}], True),
        ([{"id": "bot", "key": "@_user_1"}], True),
        ([{"id": "owner", "key": "@_user_1"}], False),
        ([{"id": "other-app", "key": "@_user_1"}], False),
        (None, False),
        ([], False),
        ([{"id": "all", "key": "@_all"}], False),
        ([{"id": "bot"}, {"id": "all", "key": "@_all"}], False),
    ],
)
def test_live_snapshot_resolves_bot_app_id_mentions(
    tmp_path: Path, snapshot_mentions: list[dict[str, str]] | None, starts_agent: bool
) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", mention=True, sender="owner")
    if snapshot_mentions is None:
        feishu.messages["request"].pop("mentions")
    else:
        feishu.messages["request"]["mentions"] = snapshot_mentions

    assert service.process_next()
    assert bool(server.prompts) is starts_agent
    if starts_agent:
        assert service.store.get_message("request")["sender_role"] == "owner_message"  # type: ignore[index]


def test_app_server_failure_marks_group_and_private_children_uncertain(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "group-request", location="thread")
    assert service.process_next() and service.process_next()
    _private(service, feishu, "private-request")
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        origins = conn.execute(
            """SELECT e.id, e.chat_id, s.id AS session_id, s.backend_session_id
               FROM coworker_events e JOIN coworker_sessions s
                 ON s.app_id = e.app_id AND s.chat_id = e.chat_id AND s.thread_id = e.topic_key
               WHERE e.message_id IN ('group-request', 'private-request')"""
        ).fetchall()
    assert len(origins) == 2
    for origin in origins:
        service.events.register_subagent(
            child_thread_id=f"child-{origin['chat_id']}",
            parent_thread_id=origin["backend_session_id"],
            agent_path="child",
            session_id=origin["session_id"],
            origin_event_id=origin["id"],
            bot_open_id="bot",
        )
    server.is_healthy = lambda: False  # type: ignore[attr-defined]
    worker = NativeCoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )

    assert not worker._server_allows_processing()
    with service.store.connect() as conn:
        statuses = dict(
            conn.execute("SELECT child_thread_id, status FROM coworker_subagents")
        )
    assert statuses == {
        "child-group": "needs_review",
        "child-private-chat": "needs_review",
    }


def test_topic_read_tool_returns_only_current_group_messages(tmp_path: Path) -> None:
    service, feishu, _ = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request")
    assert service.process_next()
    feishu.topics["same-group"] = [
        {"message_id": "m1", "chat_id": "group", "content": "visible"}
    ]
    assert (
        service.read_topic("session-1", "same-group", None)["messages"][0]["content"]
        == "visible"
    )
    feishu.topics["other-group"] = [
        {"message_id": "m2", "chat_id": "other", "content": "hidden"}
    ]
    with pytest.raises(ValueError, match="outside the current group"):
        service.read_topic("session-1", "other-group", None)
    with pytest.raises(ValueError, match="outside the current group"):
        service.read_topic("unknown-session", "same-group", None)
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                },
            },
        }
    )
    service.on_app_server_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "child", "turn": {"id": "child-turn"}},
        }
    )
    assert service.read_topic("child", "same-group", None, "child-turn", False)[
        "messages"
    ]
    with pytest.raises(ValueError, match="outside the current group"):
        service.read_topic("child", "same-group", None, "wrong-turn", False)
    feishu.group_messages = [
        {"message_id": "m3", "chat_id": "group", "content": "history"}
    ]
    assert (
        service.read_group_history("session-1", None, "turn", True)["messages"][0][
            "content"
        ]
        == "history"
    )
    assert (
        service.read_memory_history("maintenance", None, "turn", True)["messages"][0][
            "content"
        ]
        == "history"
    )
    feishu.group_messages = [
        {"message_id": "m4", "chat_id": "other", "content": "hidden"}
    ]
    with pytest.raises(ValueError, match="outside the current group"):
        service.read_memory_history("maintenance", None, "turn", True)


def test_production_can_use_existing_workspace_containing_runtime_state(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    service.config.agent_backend.working_dir = str(tmp_path)
    _receive(service, feishu, "request")
    assert service.process_next()
    assert server.sessions == [None]
    with service.store.connect() as conn:
        row = conn.execute("SELECT agent_working_dir FROM coworker_sessions").fetchone()
    assert row[0] == str(tmp_path)


def test_quote_of_confirmed_reply_triggers_once_and_unmentioned_does_not(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "first")
    assert service.process_next() and service.process_next()
    _receive(service, feishu, "quiet", mention=False)
    assert service.process_next()
    _receive(service, feishu, "quote", mention=False, parent="sent-1")
    assert service.process_next() and service.process_next()
    _receive(service, feishu, "quote", mention=False, parent="sent-1")
    assert not service.process_next()
    assert len(server.prompts) == 2
    assert feishu.sent == 2
    assert [reply["dry_run"] for reply in feishu.replies] == [True, False, True, False]


def test_changed_source_is_not_sent_and_blocks_only_that_topic(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "first")
    assert service.process_next()
    feishu.messages["first"]["content"] = "edited"
    assert service.process_next()
    assert len(feishu.replies) == 1 and feishu.replies[0]["dry_run"]
    _receive(service, feishu, "later")
    assert service.process_next()
    _receive(service, feishu, "other-topic", location="thread")
    assert service.process_next() and service.process_next()
    assert len(server.prompts) == 2
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries ORDER BY id"
            ).fetchone()[0]
            == "needs_review"
        )


@pytest.mark.parametrize(
    "change", ["remove-mention", "add-mention", "swap-mentions", "update-time"]
)
def test_reply_rechecks_mention_and_update_time_with_same_text(
    tmp_path: Path, change: str
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request", text="@同事 请现在检查")
    snapshot = feishu.messages["request"]
    snapshot["mentions"] = [{"id": "app", "key": "@_user_1", "name": "同事"}]
    if change == "swap-mentions":
        snapshot["mentions"].append(
            {"id": "other-app", "key": "@_user_2", "name": "同事"}
        )
    if change != "update-time":
        snapshot["update_time"] = "2026-11-19 17:00"
    assert service.process_next()

    if change == "remove-mention":
        snapshot.pop("mentions")
    elif change == "add-mention":
        snapshot["mentions"].append({"id": "other-app", "key": "@_user_2"})
    elif change == "swap-mentions":
        snapshot["mentions"][0]["id"], snapshot["mentions"][1]["id"] = (
            snapshot["mentions"][1]["id"],
            snapshot["mentions"][0]["id"],
        )
    else:
        snapshot["update_time"] = "2026-11-19 17:00"
    assert service.process_next()

    assert feishu.sent == 0
    with service.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM coworker_native_deliveries"
        ).fetchone()[0]
    assert status == "needs_review"


@pytest.mark.parametrize("version", [1, 2])
def test_pending_reply_accepts_previous_snapshot_fingerprint(
    tmp_path: Path, version: int
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request")
    assert service.process_next()
    legacy_fingerprint = message_content_fingerprint(
        feishu.messages["request"], include_mentions=version == 2, mention_version=2
    )
    with service.store.connect() as conn:
        # A delivery written before per-delivery snapshots uses the old evidence.
        conn.execute(
            "UPDATE coworker_native_deliveries SET source_snapshot_json = NULL"
        )
        conn.execute(
            """UPDATE messages SET raw_json = json_set(raw_json,
                 '$.coworker_source_content_sha256', ?) WHERE message_id = 'request'""",
            (legacy_fingerprint,),
        )

    assert service.process_next()
    assert feishu.sent == 1


def test_pending_reply_keeps_its_snapshot_when_shared_message_changes(
    tmp_path: Path,
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request")
    assert service.process_next()
    feishu.messages["request"].pop("mentions")
    new_fingerprint = message_content_fingerprint(
        feishu.messages["request"], include_mentions=True
    )
    with service.store.connect() as conn:
        conn.execute(
            """UPDATE messages SET raw_json = json_set(raw_json,
                 '$.coworker_source_content_sha256', ?) WHERE message_id = 'request'""",
            (new_fingerprint,),
        )
    assert service.process_next()
    assert feishu.sent == 0
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_native_deliveries").fetchone()[0]
            == "needs_review"
        )


def test_reply_rechecks_snapshot_after_preview(tmp_path: Path) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request")
    assert service.process_next()
    feishu.on_preview = lambda: feishu.messages["request"].pop("mentions")
    assert service.process_next()
    assert feishu.sent == 0
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_native_deliveries").fetchone()[0]
            == "needs_review"
        )


@pytest.mark.parametrize("edit_after_child", [False, True])
def test_child_completion_binds_its_own_new_source_snapshot(
    tmp_path: Path, edit_after_child: bool
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "first", location="thread")
    snapshot = feishu.messages["first"]
    snapshot["update_time"] = "2026-11-19 16:30"
    assert service.process_next() and service.process_next()
    legacy = message_content_fingerprint(snapshot)
    with service.store.connect() as conn:
        conn.execute(
            """UPDATE messages SET raw_json = json_set(raw_json,
                 '$.coworker_source_content_sha256', ?) WHERE message_id = 'first'""",
            (legacy,),
        )
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                    "agentPath": "worker",
                },
            },
        }
    )
    snapshot["update_time"] = "2026-11-19 16:31"
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {
                        "id": "child-turn",
                        "status": "completed" if method == "turn/completed" else None,
                    },
                },
            }
        )
    assert service.process_next()
    with service.store.connect() as conn:
        saved = json.loads(
            conn.execute(
                "SELECT source_snapshot_json FROM coworker_native_deliveries ORDER BY id DESC"
            ).fetchone()[0]
        )
    assert saved["coworker_source_update_time"] == "2026-11-19 16:31"
    assert saved["coworker_source_content_sha256"].startswith("v3:")
    if edit_after_child:
        snapshot.pop("mentions")
    assert service.process_next()
    assert feishu.sent == (1 if edit_after_child else 2)
    with service.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM coworker_native_deliveries ORDER BY id DESC"
        ).fetchone()[0]
    assert status == ("needs_review" if edit_after_child else "sent")


def test_existing_topic_alias_resumes_old_session(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    with service.store.connect() as conn:
        conn.execute(
            """INSERT INTO coworker_sessions(app_id, chat_id, thread_id, provider,
               backend_session_id, agent_working_dir, created_at, updated_at)
               VALUES ('app', 'group', 'root', 'codex', 'old-uuid', '/old-workspace', 'now', 'now')"""
        )
        conn.execute(
            "INSERT INTO coworker_topic_aliases VALUES ('app', 'group', 'thread', 'root')"
        )
    _receive(service, feishu, "followup", location="thread")
    assert service.process_next()
    assert server.sessions == ["old-uuid"]
    with service.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM coworker_sessions").fetchone()[0] == 1


def test_thread_root_alone_does_not_trigger_reply(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "first", location="thread")
    assert service.process_next() and service.process_next()
    _receive(
        service, feishu, "ordinary", mention=False, location="thread", root="sent-1"
    )
    assert service.process_next()
    assert len(server.prompts) == 1
    assert feishu.sent == 1


def test_policy_revocation_during_source_read_blocks_send(
    tmp_path: Path,
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "first")
    assert service.process_next()

    def revoke(_message: str) -> None:
        with service.store.connect() as conn:
            conn.execute("DELETE FROM chat_policies WHERE chat_id = 'group'")

    feishu.on_get = revoke
    assert service.process_next()
    assert len(feishu.replies) == 1 and feishu.replies[0]["dry_run"]
    assert feishu.sent == 0


def test_source_deleted_during_preview_blocks_send(tmp_path: Path) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "first")
    assert service.process_next()

    def delete_source() -> None:
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE messages SET is_deleted = 1 WHERE message_id = 'first'"
            )

    feishu.on_preview = delete_source
    assert service.process_next()
    assert len(feishu.replies) == 1 and feishu.replies[0]["dry_run"] is True
    assert feishu.sent == 0


def test_source_retry_does_not_block_a_different_topic(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(
        service,
        feishu,
        "slow",
        location="thread",
        thread="first-thread",
        root="first-root",
    )
    _receive(
        service,
        feishu,
        "fast",
        location="thread",
        thread="second-thread",
        root="second-root",
    )

    def fail_first(message: str) -> None:
        if message == "slow":
            raise OSError("unavailable")

    feishu.on_get = fail_first
    assert service.process_next()
    assert service.process_next()
    assert len(server.prompts) == 1 and "fast" in server.prompts[0]


def test_child_completion_returns_to_own_session_and_source(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "first", location="thread")
    assert service.process_next() and service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                    "agentPath": "worker",
                },
            },
        }
    )
    feishu.messages["first"]["content"] = "changed request after the child started"
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {
                        "id": "child-turn",
                        "status": "completed" if method == "turn/completed" else None,
                    },
                },
            }
        )
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, "session-1"]
    assert server.output_models[-1] is NativeReplyOutput
    assert "internal_subagent_completion" in server.prompts[1]
    assert "禁止用工具发送或回复飞书消息" in server.prompts[1]
    assert "changed request after the child started" in server.prompts[1]
    assert feishu.replies[-1]["message_id"] == "first"
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT runtime FROM coworker_events WHERE message_id LIKE 'internal:subagent:%'"
            ).fetchone()[0]
            == "native"
        )


def test_reused_child_turn_reports_to_the_new_origin(tmp_path: Path) -> None:
    service, feishu, _server = _service(tmp_path)
    _receive(service, feishu, "first", location="thread")
    assert service.process_next() and service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                },
            },
        }
    )
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {
                        "id": "child-1",
                        "status": "completed" if method == "turn/completed" else None,
                    },
                },
            }
        )
    assert service.process_next() and service.process_next()
    _receive(service, feishu, "second", location="thread")
    assert service.process_next() and service.process_next()
    service.on_app_server_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "session-1", "turn": {"id": "root-2"}},
        }
    )
    # The child notification may arrive before the parent tool result.
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {
                        "id": "child-2",
                        "status": "completed" if method == "turn/completed" else None,
                    },
                },
            }
        )
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "turnId": "root-2",
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-2",
                    "tool": "followupTask",
                    "senderThreadId": "session-1",
                    "receiverThreadIds": ["child"],
                    "status": "completed",
                },
            },
        }
    )
    assert service.process_next() and service.process_next()
    assert feishu.replies[-1]["message_id"] == "second"


@pytest.mark.parametrize("parent_closed", [False, True])
def test_running_child_supplement_uses_new_origin_even_if_it_finishes_during_call(
    tmp_path: Path, parent_closed: bool
) -> None:
    service, feishu, _server = _service(tmp_path)
    _receive(service, feishu, "first", location="thread")
    assert service.process_next() and service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                },
            },
        }
    )
    service.on_app_server_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "child", "turn": {"id": "child-turn"}},
        }
    )
    _receive(service, feishu, "second", location="thread")
    assert service.process_next() and service.process_next()
    service.on_app_server_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "session-1", "turn": {"id": "root-turn-2"}},
        }
    )
    item = {
        "type": "collabAgentToolCall",
        "id": "send-input",
        "tool": "sendInput",
        "senderThreadId": "session-1",
        "receiverThreadIds": ["child"],
    }
    service.on_app_server_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "session-1",
                "turnId": "root-turn-2",
                "item": {**item, "status": "inProgress"},
            },
        }
    )
    service.on_app_server_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "session-1",
                "turn": {"id": "root-turn-2", "status": "completed"},
            },
        }
    )
    service.on_app_server_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "child",
                "turn": {"id": "child-turn", "status": "completed"},
            },
        }
    )
    assert not service.process_next()
    if parent_closed:
        service.on_app_server_notification(
            {"method": "thread/closed", "params": {"threadId": "session-1"}}
        )
        child = service.events.subagent_context("child")
        assert child is not None and child["status"] == "needs_review"
        assert not service.process_next()
        return
    completion = {
        "method": "item/completed",
        "params": {
            "threadId": "session-1",
            "turnId": "root-turn-2",
            "item": {**item, "status": "completed"},
        },
    }
    service.on_app_server_notification(completion)
    service.on_app_server_notification(completion)
    assert ("session-1", "root-turn-2") not in service._root_turns  # pyright: ignore[reportPrivateUsage]
    assert service.process_next() and service.process_next()
    assert feishu.replies[-1]["message_id"] == "second"


def test_owner_private_forwards_start_separate_sessions_and_quote_resumes(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    _private(service, feishu, "ignored", sender="other")
    assert not service.process_next()
    _private(service, feishu, "forward-one", kind="merge_forward")
    assert service.process_next() and service.process_next()
    _private(service, feishu, "forward-two", kind="merge_forward")
    assert service.process_next() and service.process_next()
    _private(service, feishu, "continue-one", parent="forward-one")
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None, "session-1"]
    assert server.private == [True, True, True]
    assert "forwarded content forward-one" in server.prompts[2]
    assert "本轮为只读预演" in server.prompts[0]
    assert "写入私聊笔记" not in server.prompts[0]
    assert all(call["dry_run"] for call in feishu.replies)


def test_private_reply_survives_approval_inbox_reading_same_message(
    tmp_path: Path,
) -> None:
    service, feishu, _server = _service(tmp_path, dry_run=False)
    _private(service, feishu, "request")
    snapshot = feishu.messages["request"]
    snapshot["sender"] = {"id": "owner", "sender_type": "user"}
    approval_message = MessageNormalizer(owner_open_id="owner").normalize(
        snapshot, default_chat_type="p2p"
    )
    assert service.store.upsert_message_with_revision(approval_message).revision == 1
    assert service.process_next()
    service.store.upsert_message_with_revision(approval_message, confirmed_current=True)
    assert service.process_next()

    assert feishu.sent == 1
    with service.store.connect() as conn:
        delivery = conn.execute(
            "SELECT source_revision, status FROM coworker_native_deliveries"
        ).fetchone()
    assert tuple(delivery) == (1, "sent")


@pytest.mark.parametrize("previous_bot_notification", [False, True])
def test_private_quote_waits_for_uncertain_receipt_without_exhausting_retries(
    tmp_path: Path,
    previous_bot_notification: bool,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _private(service, feishu, "first")
    assert service.process_next()
    feishu.fail_actual = True

    def follow_during_send() -> None:
        feishu.on_actual = None
        _private(service, feishu, "follow", parent="sent-1")
        assert service.process_next()

    feishu.on_actual = follow_during_send
    assert service.process_next()
    for _ in range(4):
        with service.store.connect() as conn:
            conn.execute("""UPDATE coworker_events SET raw_json=json_remove(raw_json, '$._runtime.retry_after')
                            WHERE message_id='follow'""")
        assert service.process_next()
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT status, raw_json FROM coworker_events WHERE message_id='follow'"
        ).fetchone()
        assert (
            row["status"] == "queued"
            and json.loads(row["raw_json"])["_runtime"]["retry_count"] == 0
        )
        delivery = conn.execute(
            "SELECT id, status FROM coworker_native_deliveries"
        ).fetchone()
        assert delivery["status"] == "needs_review"
    feishu.fail_actual = False
    feishu.messages["history"] = {
        "message_id": "history",
        "chat_id": "private-chat",
        "content": "old request",
        "sender": {"id": "owner", "sender_type": "user"},
    }
    if previous_bot_notification:
        feishu.messages["history"]["sender"] = {"id": "app", "sender_type": "app"}
        action_id = service.store.create_owner_notification_action(
            task_id=None, payload={"type": "notification"}
        )
        with service.store.connect() as conn:
            conn.execute("UPDATE actions SET status='sent' WHERE id=?", (action_id,))
            conn.execute(
                """INSERT INTO dispatch_attempts(action_id, claim_token, status, sent_message_id, started_at)
                   VALUES (?, 'notice-claim', 'send_ok', 'history', 'now')""",
                (action_id,),
            )
    _private(service, feishu, "independent", parent="history")
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None]
    assert feishu.replies[-1]["message_id"] == "independent"
    _private(service, feishu, "continue-independent", parent="independent")
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None, "session-2"]
    service.deliveries.resolve_uncertain(
        delivery["id"], sent_message_id="sent-1", daemon_stopped=True
    )
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET raw_json=json_remove(raw_json, '$._runtime.retry_after')
                        WHERE message_id='follow'""")
    assert service.process_next()
    assert server.sessions == [None, None, "session-2", "session-1"]


def test_owner_approval_command_is_not_given_to_private_codex(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _private(service, feishu, "approval-command", text="/send abc confirmed")
    assert service.process_next()
    assert not service.process_next()
    assert not server.prompts and not feishu.replies


@pytest.mark.parametrize("changed_quote", [False, True])
def test_private_manual_send_verifies_quoted_context(
    tmp_path: Path, changed_quote: bool
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _private(service, feishu, "quoted")
    assert service.process_next() and service.process_next()
    server.outputs.append(
        {"action": "reply", "text": "draft", "approval_required": True}
    )
    _private(service, feishu, "request", parent="quoted")
    assert service.process_next()
    assert "forwarded content quoted" in server.prompts[-1]
    if changed_quote:
        feishu.messages["quoted"]["is_deleted"] = True
    summary = _dispatch_manual_reply(service, feishu)
    assert summary.sent == (0 if changed_quote else 1)
    assert feishu.sent == (1 if changed_quote else 2)
    if not changed_quote:
        assert feishu.replies[-1]["message_id"] == "request"
        assert feishu.replies[-1]["as_identity"] == "bot"
        _private(service, feishu, "continued", parent="sent-2")
        assert service.process_next()
        assert server.sessions == [None, "session-1", "session-1"]


@pytest.mark.parametrize("changed_quote", [False, True])
def test_group_quote_is_scoped_and_checked_before_delivery(
    tmp_path: Path, changed_quote: bool
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    feishu.messages["quoted"] = {
        "message_id": "quoted",
        "chat_id": "group" if changed_quote else "other-group",
        "content": "unique quote text",
        "msg_type": "text",
        "unused_transport_metadata": "raw-only",
    }
    _receive(service, feishu, "request", location="thread", parent="quoted")
    assert service.process_next()
    assert ("unique quote text" in server.prompts[-1]) == changed_quote
    assert "unused_transport_metadata" not in server.prompts[-1]
    if changed_quote:
        feishu.messages["quoted"]["content"] = "edited quote"
    assert service.process_next()
    assert feishu.sent == (0 if changed_quote else 1)


@pytest.mark.parametrize(
    "content",
    [
        '{"status":"failed"}',
        '{"text":"example","version":2}',
        {"title": "附件说明", "content": [[{"tag": "text", "text": "完整正文"}]]},
        '<file key="file_quote" name="example.pdf"/>',
    ],
)
def test_group_quote_preserves_own_body_and_author(
    tmp_path: Path, content: object
) -> None:
    service, feishu, server = _service(tmp_path)
    feishu.messages["quoted"] = {
        "message_id": "quoted",
        "chat_id": "group",
        "content": content,
        "msg_type": "text",
        "sender": {"id": "author", "name": "引用作者"},
        "thread_replies": [
            {"message_id": "reply", "content": {"file_key": "file_unrelated"}}
        ],
    }
    _receive(service, feishu, "request", location="thread", parent="quoted")
    assert service.process_next()
    quote_body = server.prompts[-1].split("\n引用内容：\n```json\n", 1)[1]
    quote = json.loads(quote_body.removesuffix("\n```"))
    assert quote["message_id"] == "quoted" and quote["content"] == content
    assert quote["sender_id"] == "author" and quote["sender_name"] == "引用作者"
    assert "file_unrelated" not in server.prompts[-1]


@pytest.mark.parametrize("root_silent", [False, True])
def test_child_result_keeps_root_quote_snapshot(
    tmp_path: Path, root_silent: bool
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    feishu.messages["quoted"] = {
        "message_id": "quoted",
        "chat_id": "group",
        "content": "source to verify",
        "msg_type": "text",
    }
    server.outputs.append(
        {"action": "silent" if root_silent else "reply", "text": "root result"}
    )
    _receive(service, feishu, "request", location="thread", parent="quoted")
    assert service.process_next()
    if not root_silent:
        assert service.process_next() and feishu.sent == 1
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                },
            },
        }
    )
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {"id": "child-turn", "status": "completed"},
                },
            }
        )
    if root_silent:
        feishu.messages["quoted"]["is_deleted"] = True
        assert service.process_next()
        assert len(server.prompts) == 1 and not feishu.replies
        return
    server.outputs.append(
        {"action": "reply", "text": "child result", "approval_required": True}
    )
    assert service.process_next()
    assert len(server.prompts) == 2
    feishu.messages["quoted"]["content"] = "edited quote"
    summary = _dispatch_manual_reply(service, feishu)
    assert summary.sent == 0 and feishu.sent == 1


def test_delivery_retries_temporary_source_read_before_actual_reply(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    _receive(service, feishu, "request", location="thread")
    assert service.process_next()

    def fail_first_read(message_id: str) -> None:
        assert message_id == "request"
        feishu.on_get = None
        raise TimeoutError("first source read timed out")

    feishu.on_get = fail_first_read
    assert service.process_next()
    assert len(server.prompts) == 1 and feishu.sent == 1
    preview, actual = feishu.replies
    assert preview["dry_run"] and not actual["dry_run"]
    assert preview["idempotency_key"] == actual["idempotency_key"]


def test_owner_can_watch_real_topic_and_expiry_stops_unmentioned_messages(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append(
        {"action": "watch", "text": "watching", "watch_duration_seconds": 3600}
    )
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    assert server.output_models[-1] is NativeWatchOutput
    server.outputs.append({"action": "silent", "text": ""})
    _receive(service, feishu, "update", mention=False, location="thread")
    assert service.process_next()
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET received_at = '2000-01-01T00:00:00Z',
                     raw_json = json_remove(raw_json, '$._runtime.retry_after') WHERE status = 'queued'""")
    assert service.process_next()
    assert len(server.prompts) == 2 and len(feishu.replies) == 1
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE coworker_native_watches SET expires_at = '2000-01-01T00:00:00Z'"
        )
    _receive(service, feishu, "expired", mention=False, location="thread")
    assert service.process_next()
    assert len(server.prompts) == 2
    _receive(service, feishu, "mentioned", location="thread")
    assert service.process_next() and service.process_next()
    assert len(server.prompts) == 3


@pytest.mark.parametrize("action", ["watch", "unwatch"])
def test_changed_owner_message_cannot_change_watch(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    if action == "unwatch":
        server.outputs.append(
            {"action": "watch", "text": "watching", "watch_duration_seconds": 3600}
        )
        _receive(service, feishu, "first", location="thread", sender="owner")
        assert service.process_next() and service.process_next()
        feishu.replies.clear()
    server.outputs.append(
        {"action": action, "text": "confirmed", "watch_duration_seconds": 3600}
    )
    original = server.run_turn

    def edit_after_turn(*args: Any, **kwargs: Any) -> AgentRunResult:
        result = original(*args, **kwargs)
        feishu.messages["watch-request"]["is_deleted"] = True
        return result

    monkeypatch.setattr(server, "run_turn", edit_after_turn)
    _receive(service, feishu, "watch-request", location="thread", sender="owner")
    assert service.process_next()
    assert not service.process_next()
    with service.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM coworker_native_watches").fetchone()[
            0
        ] == (1 if action == "unwatch" else 0)
    assert not feishu.replies


def test_watched_message_burst_is_judged_in_one_turn(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    _receive(
        service, feishu, "update-one", mention=False, location="thread", parent="root"
    )
    _receive(
        service, feishu, "update-two", mention=False, location="thread", parent="root"
    )
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET received_at = '2000-01-01T00:00:00Z',
                     raw_json = json_remove(raw_json, '$._runtime.retry_after') WHERE status = 'queued'""")
    server.outputs.append({"action": "silent", "text": ""})
    assert service.process_next()
    assert len(server.prompts) == 2
    assert "update-one" in server.prompts[1] and "update-two" in server.prompts[1]
    with service.store.connect() as conn:
        rows = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id IN ('update-one', 'update-two')"
        ).fetchall()
        assert [row[0] for row in rows] == ["completed", "completed"]


def test_independent_private_requests_continue_when_another_source_retries(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    _private(service, feishu, "slow")
    _private(service, feishu, "fast")

    def unavailable(message_id: str) -> None:
        if message_id == "slow":
            raise OSError("source unavailable")

    feishu.on_get = unavailable
    assert service.process_next() and service.process_next()
    assert len(server.prompts) == 1 and "fast" in server.prompts[0]


def test_changed_watched_context_blocks_batch_delivery(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    _receive(service, feishu, "update-one", mention=False, location="thread")
    _receive(service, feishu, "update-two", mention=False, location="thread")
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET received_at = '2000-01-01T00:00:00Z',
                     raw_json = json_remove(raw_json, '$._runtime.retry_after') WHERE status = 'queued'""")
    assert service.process_next()
    feishu.messages["update-two"]["content"] = "changed"
    assert service.process_next()
    assert len(feishu.replies) == 2 and all(r["dry_run"] for r in feishu.replies)
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries ORDER BY id DESC"
            ).fetchone()[0]
            == "needs_review"
        )


@pytest.mark.parametrize("parent_action", ["silent", "reply"])
@pytest.mark.parametrize("change", [False, True])
def test_watched_batch_context_follows_child_completion(
    tmp_path: Path, parent_action: str, change: bool
) -> None:
    service, feishu, _ = _service(tmp_path, dry_run=False)

    class DelegatingServer(FakeServer):
        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            result = super().run_turn(prompt, **kwargs)
            if "watched_updates" in prompt:
                service.on_app_server_notification(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": result.session_id,
                            "item": {
                                "type": "subAgentActivity",
                                "kind": "started",
                                "agentThreadId": "child",
                            },
                        },
                    }
                )
                return replace(
                    result, json_data={"action": parent_action, "text": "已接单"}
                )
            if "子 Agent 已结束" in prompt:
                return replace(
                    result, json_data={"action": "reply", "text": "已检查 2.0"}
                )
            return result

    server = DelegatingServer()
    service.app_server = server  # type: ignore[assignment]
    server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    _receive(
        service, feishu, "first", mention=False, location="thread", text="讨论版本"
    )
    _receive(
        service, feishu, "second", mention=False, location="thread", text="版本是 2.0"
    )
    assert service.process_next(locating_only=True) and service.process_next(
        locating_only=True
    )
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET received_at = '2000-01-01T00:00:00Z' WHERE status = 'queued'"
        )
    assert service.process_next()
    if parent_action == "reply":
        assert service.process_next()
    elif change:
        feishu.messages["second"]["content"] = "版本改为 3.0"
    if parent_action == "silent" and not change:
        service.events.scrub_expired_payloads(
            cutoff="2020-01-01T00:00:00Z", dry_run=False, protect_pending=True
        )
        with service.store.connect() as conn:
            assert conn.execute(
                "SELECT raw_json FROM coworker_events WHERE message_id = 'first'"
            ).fetchone()[0]
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {"id": "turn", "status": "completed"},
                },
            }
        )
    previous_sent = feishu.sent
    if parent_action == "silent" and not change:

        def scrub_during_completion(_message_id: str) -> None:
            feishu.on_get = None
            service.events.scrub_expired_payloads(
                cutoff="2020-01-01T00:00:00Z", dry_run=False, protect_pending=True
            )
            with service.store.connect() as conn:
                assert conn.execute(
                    "SELECT raw_json FROM coworker_events WHERE message_id = 'first'"
                ).fetchone()[0]

        feishu.on_get = scrub_during_completion
    assert service.process_next()
    if change and parent_action == "silent":
        with service.store.connect() as conn:
            status = conn.execute(
                "SELECT status FROM coworker_events WHERE message_id LIKE 'internal:subagent:%'"
            ).fetchone()[0]
        assert status == "needs_review" and feishu.sent == previous_sent
        return
    if change:
        feishu.messages["second"]["content"] = "版本改为 3.0"
    assert service.process_next()
    assert feishu.sent == previous_sent + (0 if change else 1)
    with service.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM coworker_native_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert status == ("needs_review" if change else "sent")
    if parent_action == "silent" and not change:
        service.events.scrub_expired_payloads(
            cutoff="2020-01-01T00:00:00Z", dry_run=False, protect_pending=True
        )
        with service.store.connect() as conn:
            origin = conn.execute(
                "SELECT status, raw_json FROM coworker_events WHERE message_id = 'first'"
            ).fetchone()
        assert tuple(origin) == ("completed", None)
        _receive(service, feishu, "later", location="thread")
        assert service.process_next() and service.process_next()


def test_non_owner_cannot_enable_watch(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "try-watch", location="thread")
    assert service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM coworker_native_watches").fetchone()[0]
            == 0
        )


def test_native_reply_requiring_owner_uses_existing_approval_queue(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append(
        {
            "action": "reply",
            "text": "确认后答复",
            "approval_required": True,
            "approval_reason": "需要确认承诺",
        }
    )
    _receive(service, feishu, "request")
    assert service.process_next()
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT short_id, payload_json FROM approvals WHERE status = 'pending'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert (
            payload["identity"] == "bot"
            and payload["reply_destination"]["location"] == "main"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM coworker_native_deliveries").fetchone()[
                0
            ]
            == 0
        )
    assert not feishu.replies
    result = OperatorCommandService(service.store).approve(
        row["short_id"], actor="owner", execution_mode="dry_run"
    )
    assert result.status == "applied"


def test_child_approval_with_supplement_requires_manual_send(tmp_path: Path) -> None:
    service, feishu, _ = _service(tmp_path, dry_run=False)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="main")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        service.on_app_server_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "active-session",
                    "item": {
                        "type": "subAgentActivity",
                        "kind": "started",
                        "agentThreadId": "child",
                    },
                },
            }
        )
        _receive(
            service,
            feishu,
            "supplement",
            location="main",
            parent="request",
            mention=False,
            text="补充：只检查 2.0",
        )
        assert service.process_next(locating_only=True) and service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert service.process_next()
    server.approval = True
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "child",
                    "turn": {"id": "turn", "status": "completed"},
                },
            }
        )
    assert service.process_next()
    with service.store.connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM approvals WHERE status = 'pending'"
            ).fetchone()[0]
        )
    assert payload["approvable"] is False


def test_disabled_group_auto_reply_requests_approval(tmp_path: Path) -> None:
    service, feishu, _server = _service(tmp_path)
    service.store.upsert_chat_product_policy(
        {
            "chat_id": "group",
            "auto_reply": False,
            "bot_joined": True,
            "reply_identity": "bot",
            "allow_user_fallback": False,
        },
        actor="test",
    )
    _receive(service, feishu, "request")
    assert service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM coworker_native_deliveries").fetchone()[
                0
            ]
            == 0
        )
    assert not feishu.replies


def test_unwatch_takes_effect_when_group_auto_reply_is_disabled(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    service.store.upsert_chat_product_policy(
        {
            "chat_id": "group",
            "auto_reply": False,
            "bot_joined": True,
            "reply_identity": "bot",
            "allow_user_fallback": False,
        },
        actor="test",
    )
    server.outputs.append({"action": "unwatch", "text": "stopped"})
    _receive(service, feishu, "unwatch", location="thread", sender="owner")
    assert service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM coworker_native_watches").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM approvals WHERE status = 'pending'"
            ).fetchone()[0]
            == 1
        )


def test_disabled_private_auto_reply_requests_manual_approval(tmp_path: Path) -> None:
    service, feishu, _ = _service(tmp_path)
    policy = service.store.get_product_policy()
    assert policy is not None
    policy["reply_policy"]["p2p_auto_reply"] = False
    service.store.update_product_policy(policy, actor="test")
    _private(service, feishu, "request")
    assert service.process_next()
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM approvals WHERE status = 'pending'"
        ).fetchone()
        assert row is not None and json.loads(row[0])["approvable"] is False
        assert (
            conn.execute("SELECT count(*) FROM coworker_native_deliveries").fetchone()[
                0
            ]
            == 0
        )
    assert not feishu.replies


def test_quote_read_failure_before_agent_is_retried(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", parent="quoted")

    def fail_quote(message_id: str) -> None:
        if message_id == "quoted":
            raise OSError("temporarily unavailable")

    feishu.on_get = fail_quote
    assert service.process_next()
    assert not server.prompts
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT status, raw_json FROM coworker_events WHERE message_id='request'"
        ).fetchone()
        assert row["status"] == "queued"
        assert json.loads(row["raw_json"])["_runtime"]["retry_count"] == 1
        conn.execute(
            "UPDATE coworker_events SET raw_json = json_remove(raw_json, '$._runtime.retry_after') WHERE message_id='request'"
        )
    feishu.on_get = None
    assert service.process_next()
    assert len(server.prompts) == 1


def test_quote_of_legacy_bot_reply_continues_matching_topic(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "legacy-source", location="thread")
    assert service.process_next()
    with service.store.connect() as conn:
        conn.execute("DELETE FROM coworker_native_deliveries")
        conn.execute(
            "UPDATE coworker_events SET runtime='legacy' WHERE message_id='legacy-source'"
        )
        action = conn.execute(
            """INSERT INTO actions(idempotency_key, kind, status, source_message_id,
               execution_mode, payload_json, created_at, updated_at)
               VALUES ('legacy-action', 'send_reply', 'sent', 'legacy-source', 'production',
               '{"identity":"bot","business_mode":"coworker","reply_destination":{"chat_id":"group","location":"thread","thread_id":"thread"}}', 'now', 'now')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO dispatch_attempts(action_id, claim_token, status, sent_message_id, started_at)
               VALUES (?, 'legacy-claim', 'send_ok', 'legacy-reply', 'now')""",
            (action,),
        )
    feishu.messages["legacy-reply"] = {
        "message_id": "legacy-reply",
        "chat_id": "group",
        "content": "old answer",
    }
    _receive(
        service,
        feishu,
        "followup",
        mention=False,
        location="thread",
        parent="legacy-reply",
    )
    assert service.process_next()
    assert len(server.prompts) == 2 and server.sessions[-1] == "session-1"


def test_legacy_reply_destination_can_differ_from_source_location(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "legacy-source", location="main")
    assert service.process_next()
    with service.store.connect() as conn:
        conn.execute("DELETE FROM coworker_native_deliveries")
        conn.execute(
            "UPDATE coworker_events SET runtime='legacy' WHERE message_id='legacy-source'"
        )
        action = conn.execute(
            """INSERT INTO actions(idempotency_key, kind, status, source_message_id,
               execution_mode, payload_json, created_at, updated_at)
               VALUES ('legacy-action', 'send_reply', 'sent', 'legacy-source', 'production',
               '{"identity":"bot","business_mode":"coworker","reply_destination":{"chat_id":"group","location":"thread","thread_id":"thread"}}', 'now', 'now')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO dispatch_attempts(action_id, claim_token, status, sent_message_id, started_at)
               VALUES (?, 'legacy-claim', 'send_ok', 'legacy-reply', 'now')""",
            (action,),
        )
    feishu.messages["legacy-reply"] = {
        "message_id": "legacy-reply",
        "chat_id": "group",
        "content": "old answer",
    }
    _receive(
        service,
        feishu,
        "followup",
        mention=False,
        location="thread",
        parent="legacy-reply",
    )
    assert service.process_next()
    assert len(server.prompts) == 2 and server.sessions[-1] is None


def test_topic_mode_approval_does_not_approve_a_main_chat_reply(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    service.reply_mode = "topic"
    server.outputs.append(
        {"action": "reply", "text": "draft", "approval_required": True}
    )
    _receive(service, feishu, "request")
    assert service.process_next()
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM approvals WHERE status='pending'"
        ).fetchone()
        assert row is not None and json.loads(row[0])["approvable"] is False
        assert (
            conn.execute("SELECT count(*) FROM coworker_native_deliveries").fetchone()[
                0
            ]
            == 0
        )


def test_handoff_pauses_one_topic_and_reloads_before_backlog(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "one", location="thread")
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        session_id = conn.execute("SELECT id FROM coworker_sessions").fetchone()[0]
    service.native_state.request_handoff(session_id)
    assert service.process_next()
    assert server.released == ["session-1"]
    status = service.events.status()["native_handoffs"][0]
    assert (
        status["status"] == "ready"
        and "codex --no-daemon resume session-1" in status["operator_command"]
    )
    _receive(service, feishu, "blocked", location="thread")
    _receive(
        service,
        feishu,
        "other",
        location="thread",
        thread="other-thread",
        root="other-root",
    )
    assert service.process_next() and service.process_next() and service.process_next()
    assert len(server.prompts) == 2
    service.native_state.request_handoff(session_id, resume=True)
    assert service.process_next()
    assert server.reloaded == [("session-1", True, str(tmp_path / "workspace"))]
    assert service.process_next() and service.process_next()
    assert server.sessions[-1] == "session-1"


def test_handoff_drains_child_even_when_user_backlog_arrived_first(
    tmp_path: Path,
) -> None:
    service, feishu, _server = _service(tmp_path)
    _receive(service, feishu, "one", location="thread")
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        session_id = conn.execute("SELECT id FROM coworker_sessions").fetchone()[0]
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "child",
                },
            },
        }
    )
    service.on_app_server_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "child", "turn": {"id": "child-turn"}},
        }
    )
    service.native_state.request_handoff(session_id)
    _receive(service, feishu, "backlog", location="thread")
    assert service.process_next()
    service.on_app_server_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "child",
                "turn": {"id": "child-turn", "status": "completed"},
            },
        }
    )
    assert service.process_next() and service.process_next() and service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_native_handoffs").fetchone()[0]
            == "ready"
        )
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id='backlog'"
            ).fetchone()[0]
            == "queued"
        )


def test_only_explicit_supplement_to_current_request_is_steered(tmp_path: Path) -> None:
    service, feishu, _ = _service(tmp_path)

    class BlockingServer(FakeServer):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.steers: list[str] = []

        def run_turn(
            self,
            prompt: str,
            *,
            session_id: str | None,
            cwd: str,
            output_model: object,
            on_thread: Any,
            instructions: str = "",
            private: bool = False,
            unload_delay_seconds: int | None = None,
            dry_run_safety: bool = False,
            turn_timeout_seconds: float | None = None,
            ephemeral: bool = False,
        ) -> AgentRunResult:
            if output_model is SteerDecision:
                return super().run_turn(
                    prompt,
                    session_id=session_id,
                    cwd=cwd,
                    output_model=output_model,
                    on_thread=on_thread,
                    instructions=instructions,
                    private=private,
                    unload_delay_seconds=unload_delay_seconds,
                    dry_run_safety=dry_run_safety,
                    turn_timeout_seconds=turn_timeout_seconds,
                    ephemeral=ephemeral,
                )
            if not self.started.is_set():
                on_thread("active-session")
                self.started.set()
                assert self.release.wait(timeout=5)
            return AgentRunResult(
                argv=[],
                exit_code=0,
                session_id="active-session",
                json_data={"action": "reply", "text": "answer"},
            )

        def active_turn_id(self, _thread_id: str) -> str | None:
            return (
                "active-turn"
                if self.started.is_set() and not self.release.is_set()
                else None
            )

        def steer(
            self, _thread_id: str, prompt: str, *, expected_turn_id: str | None = None
        ) -> None:
            assert expected_turn_id == "active-turn"
            self.steers.append(prompt)

    server = BlockingServer()
    server.decisions = ["current", "unrelated"]
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "one", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(
            service,
            feishu,
            "supplement",
            location="thread",
            parent="one",
            text="补充：截止周五",
        )
        assert service.process_next()
        assert service.process_next_steer()
        with service.store.connect() as conn:
            assert (
                conn.execute("SELECT status FROM coworker_native_steers").fetchone()[0]
                == "accepted"
            )
            assert (
                conn.execute(
                    "SELECT status FROM coworker_events WHERE message_id='one'"
                ).fetchone()[0]
                == "processing"
            )
        _receive(
            service,
            feishu,
            "unrelated",
            location="thread",
            parent="one",
            text="另做一个任务",
        )
        assert service.process_next()
        assert not service.process_next_steer()
        assert len(server.steers) == 1
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE messages SET is_deleted = 1, revision = revision + 1 WHERE message_id = 'supplement'"
            )
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id='one'"
            ).fetchone()[0]
            == "needs_review"
        )
    assert not feishu.replies


def test_queued_unrelated_request_does_not_block_later_supplement(
    tmp_path: Path,
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "one", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(service, feishu, "unrelated", location="thread", text="另做一个任务")
        assert service.process_next()
        assert not service.process_next_steer()
        _receive(
            service,
            feishu,
            "supplement",
            location="thread",
            parent="one",
            mention=False,
            text="补充：截止周五",
        )
        assert service.process_next()
        assert service.process_next_steer()
        assert len(server.steers) == 1 and "截止周五" in server.steers[0]
        with service.store.connect() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM coworker_events WHERE message_id = 'unrelated'"
                ).fetchone()[0]
                == "queued"
            )
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    assert any("另做一个任务" in prompt for prompt in server.prompts)


@pytest.mark.parametrize("decision", ["current", "unrelated", "uncertain", "invalid"])
@pytest.mark.parametrize("enqueued_before_start", [False, True])
def test_natural_followup_is_classified_without_losing_queued_messages(
    tmp_path: Path, decision: str, enqueued_before_start: bool
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    server.decisions = [decision]
    service.app_server = server  # type: ignore[assignment]
    followup = (
        "另做一个独立任务" if decision == "unrelated" else "刚才版本说错了，应该是 2.0"
    )
    _receive(service, feishu, "one", location="thread", text="检查 1.0 的发布")

    def receive_followup() -> None:
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="one",
            mention=False,
            text=followup,
        )

    if enqueued_before_start:
        receive_followup()
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        if not enqueued_before_start:
            receive_followup()
        assert service.process_next()
        assert service.process_next_steer() is (decision == "current")
        assert "检查 1.0 的发布" in server.classification_prompts[0]
        assert followup in server.classification_prompts[0]
        assert bool(server.steers) is (decision == "current")
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    if decision != "current":
        assert any(followup in prompt for prompt in server.prompts)


def test_queued_request_and_its_followup_both_reach_the_model(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", location="thread", text="检查 1.0")
    _receive(
        service,
        feishu,
        "followup",
        location="thread",
        parent="request",
        mention=False,
        text="更正：应该检查 2.0",
    )
    while service.process_next():
        pass
    assert len(server.prompts) == 2 and "应该检查 2.0" in server.prompts[1]


def test_followup_arriving_before_reply_receipt_is_not_lost(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", location="thread", text="检查 1.0")
    assert service.process_next()
    _receive(
        service,
        feishu,
        "followup",
        location="thread",
        parent="request",
        mention=False,
        text="更正：应该检查 2.0",
    )
    while service.process_next():
        pass
    assert len(server.prompts) == 2 and "应该检查 2.0" in server.prompts[1]


@pytest.mark.parametrize("same_topic", [True, False])
def test_pending_transferred_reply_retains_source_for_followups(
    tmp_path: Path, same_topic: bool
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request", location="main", text="检查 1.0")
    assert service.process_next() and service.process_next() and service.process_next()
    _receive(
        service,
        feishu,
        "followup",
        location="thread",
        thread="opened-thread" if same_topic else "other-thread",
        parent="request",
        mention=False,
        text="更正：应该检查 2.0",
    )
    while service.process_next():
        pass
    assert any("应该检查 2.0" in prompt for prompt in server.prompts) is same_topic


def test_reply_to_old_user_request_after_delivery_stays_silent(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", location="thread")
    assert service.process_next() and service.process_next()
    _receive(
        service, feishu, "later", location="thread", parent="request", mention=False
    )
    while service.process_next():
        pass
    assert len(server.prompts) == 1


def test_reply_to_an_unaddressed_group_message_stays_silent(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "ordinary", location="thread", mention=False)
    _receive(
        service,
        feishu,
        "reply",
        location="thread",
        parent="ordinary",
        mention=False,
    )
    while service.process_next():
        pass
    assert not server.prompts


@pytest.mark.parametrize("change", ["text", "mentions"])
def test_followup_changed_during_classification_is_not_steered_from_old_text(
    tmp_path: Path, change: str
) -> None:
    service, feishu, _ = _service(tmp_path)

    class EditingServer(BlockingNativeServer):
        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            result = super().run_turn(prompt, **kwargs)
            if kwargs["output_model"] is SteerDecision:
                if change == "text":
                    feishu.messages["followup"]["content"] = "版本应为 3.0"
                else:
                    mentions = feishu.messages["followup"]["mentions"]
                    mentions[0]["id"], mentions[1]["id"] = (
                        mentions[1]["id"],
                        mentions[0]["id"],
                    )
            return result

    server = EditingServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "one", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="one",
            mention=False,
            text="版本应为 2.0",
        )
        if change == "mentions":
            feishu.messages["followup"]["mentions"] = [
                {"id": "u1", "key": "@_user_1", "name": "同事"},
                {"id": "u2", "key": "@_user_2", "name": "同事"},
            ]
        assert service.process_next()
        assert not service.process_next_steer()
        assert not server.steers
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        state = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id = 'followup'"
        ).fetchone()[0]
    assert state == "completed"
    if change == "text":
        assert any("版本应为 3.0" in prompt for prompt in server.prompts)


def test_followup_classification_preserves_old_direct_quote_and_can_use_full_context(
    tmp_path: Path,
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        for index in range(6):
            _receive(
                service,
                feishu,
                f"update-{index}",
                location="thread",
                parent="request",
                mention=False,
                text=f"补充线索 {index}",
            )
            assert service.process_next() and service.process_next_steer()

        server.decisions.extend(["uncertain", "current"])
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="update-0",
            mention=False,
            text="确认使用这条补充。",
        )
        assert service.process_next() and service.process_next_steer()
        compact, full = [
            json.loads(prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
            for prompt in server.classification_prompts[-2:]
        ]
        assert compact["new_message"]["reply_to_message_id"] == "update-0"
        assert [item["message_id"] for item in compact["accepted_supplements"]] == [
            "update-0",
            "update-2",
            "update-3",
            "update-4",
            "update-5",
        ]
        assert [item["message_id"] for item in full["accepted_supplements"]] == [
            f"update-{index}" for index in range(6)
        ]
        assert "确认使用这条补充" in server.steers[-1]
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.parametrize("finish_before_steer", [False, True])
def test_reply_to_an_accepted_supplement_continues_the_same_task(
    tmp_path: Path, finish_before_steer: bool
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(
            service,
            feishu,
            "first",
            location="thread",
            parent="request",
            mention=False,
            text="补充：只检查 2.0",
        )
        assert service.process_next() and service.process_next_steer()
        _receive(
            service,
            feishu,
            "second",
            location="thread",
            parent="first",
            mention=False,
            text="刚才更正为 3.0",
        )
        assert service.process_next()
        if not finish_before_steer:
            assert service.process_next_steer()
            assert "只检查 2.0" in server.classification_prompts[-1]
        else:
            server.release.set()
            assert not service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    assert any("更正为 3.0" in text for text in server.prompts + server.steers)
    with service.store.connect() as conn:
        state = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id = 'second'"
        ).fetchone()[0]
    assert state == "completed"


def test_reply_to_supplement_waiting_for_steer_receipt_is_not_lost(
    tmp_path: Path,
) -> None:
    class WaitingServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.sending = threading.Event()
            self.confirm = threading.Event()

        def steer(
            self, _thread_id: str, prompt: str, *, expected_turn_id: str | None = None
        ) -> None:
            assert expected_turn_id == "active-turn"
            self.steers.append(prompt)
            self.sending.set()
            assert self.confirm.wait(timeout=5)

    service, feishu, _ = _service(tmp_path)
    server = WaitingServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    steer_worker: threading.Thread | None = None
    try:
        _receive(
            service, feishu, "first", location="thread", parent="request", mention=False
        )
        assert service.process_next()
        steer_worker = threading.Thread(target=service.process_next_steer)
        steer_worker.start()
        assert server.sending.wait(timeout=3)
        _receive(
            service,
            feishu,
            "second",
            location="thread",
            parent="first",
            mention=False,
            text="刚才更正为 3.0",
        )
        assert service.process_next()
        server.release.set()
        server.confirm.set()
        steer_worker.join(timeout=5)
        assert not steer_worker.is_alive()
        assert not service.process_next_steer()
    finally:
        server.release.set()
        server.confirm.set()
        if steer_worker is not None:
            steer_worker.join(timeout=5)
        worker.join(timeout=5)
    assert (
        not worker.is_alive()
        and steer_worker is not None
        and not steer_worker.is_alive()
    )
    while service.process_next():
        pass
    assert any("更正为 3.0" in prompt for prompt in server.prompts)


def test_queued_chain_survives_when_only_first_supplement_is_steered(
    tmp_path: Path,
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread", text="检查 1.0")
    _receive(
        service,
        feishu,
        "first",
        location="thread",
        parent="request",
        mention=False,
        text="补充：检查 2.0",
    )
    _receive(
        service,
        feishu,
        "second",
        location="thread",
        parent="first",
        mention=False,
        text="更正：应该检查 3.0",
    )
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        assert service.process_next() and service.process_next()
        assert service.process_next_steer()
        server.release.set()
        assert not service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    assert any("应该检查 3.0" in prompt for prompt in server.prompts)


@pytest.mark.parametrize("late_skip", [False, True])
def test_followup_checked_again_when_its_queued_parent_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_skip: bool
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    server.decisions = ["unrelated", "current"]
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "first", location="thread", text="原任务")
    first = threading.Thread(target=service.process_next)
    first.start()
    assert server.started.wait(timeout=3)
    second: threading.Thread | None = None
    stale_check: threading.Thread | None = None
    skip_entered, release_skip = threading.Event(), threading.Event()
    skip = service.events.skip_native_steer
    try:
        _receive(
            service,
            feishu,
            "new-task",
            location="thread",
            parent="first",
            mention=False,
            text="另一个任务：检查版本 1.0",
        )
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="new-task",
            mention=False,
            text="更正为 2.0",
        )
        assert service.process_next(locating_only=True)
        assert service.process_next(locating_only=True)
        assert not service.process_next_steer()  # New task is unrelated to the first.
        if late_skip:
            with service.store.connect() as conn:
                correction_id = conn.execute(
                    "SELECT id FROM coworker_events WHERE message_id = 'correction'"
                ).fetchone()[0]

            def delay_skip(event_id: int) -> None:
                if event_id == correction_id:
                    skip_entered.set()
                    assert release_skip.wait(timeout=5)
                skip(event_id)

            monkeypatch.setattr(service.events, "skip_native_steer", delay_skip)
            stale_check = threading.Thread(target=service.process_next_steer)
            stale_check.start()
            assert skip_entered.wait(timeout=3)
        else:
            assert not service.process_next_steer()  # Correction is not for the first.
        server.release.set()
        first.join(timeout=5)
        assert not first.is_alive()
        assert service.process_next()  # Preview the first reply.

        server.release.clear()
        server.started.clear()
        second = threading.Thread(target=service.process_next)
        second.start()
        assert server.started.wait(timeout=3)
        if stale_check is not None:
            release_skip.set()
            stale_check.join(timeout=5)
            assert not stale_check.is_alive()
            monkeypatch.setattr(service.events, "skip_native_steer", skip)
        assert service.process_next_steer()
        assert "更正为 2.0" in server.steers[-1]
    finally:
        release_skip.set()
        server.release.set()
        first.join(timeout=5)
        if stale_check is not None:
            stale_check.join(timeout=5)
        if second is not None:
            second.join(timeout=5)
    assert second is not None and not second.is_alive()


def test_followup_is_processed_if_current_turn_finishes_before_steering(
    tmp_path: Path,
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "one", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="one",
            mention=False,
            text="刚才版本说错了，应该是 2.0",
        )
        assert service.process_next()
        server.release.set()
        assert not service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    assert any("应该是 2.0" in prompt for prompt in server.prompts)


@pytest.mark.parametrize("decision", ["current", "unrelated", "uncertain"])
def test_followup_classified_after_turn_end_suppresses_only_stale_reply(
    tmp_path: Path, decision: str
) -> None:
    class RacingServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.classifying = threading.Event()
            self.finish_classifying = threading.Event()
            self.original_returned = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                self.classifying.set()
                assert self.finish_classifying.wait(timeout=5)
                return FakeServer.run_turn(self, prompt, **kwargs)
            result = super().run_turn(prompt, **kwargs)
            self.original_returned.set()
            return result

        def steer(
            self, _thread_id: str, prompt: str, *, expected_turn_id: str | None = None
        ) -> None:
            assert prompt
            raise TurnNotActive("turn already finished")

    service, feishu, _ = _service(tmp_path)
    server = RacingServer()
    server.decisions = [decision]
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread", text="检查 1.0")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    classifier: threading.Thread | None = None
    try:
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
            text="应该检查 2.0",
        )
        assert service.process_next()
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert server.classifying.wait(timeout=3)
        server.release.set()
        assert server.original_returned.wait(timeout=3)
        worker.join(timeout=0.5)
    finally:
        server.finish_classifying.set()
        if classifier is not None:
            classifier.join(timeout=5)
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        deliveries = conn.execute(
            """SELECT e.message_id FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id ORDER BY d.id"""
        ).fetchall()
    assert [row[0] for row in deliveries] == (
        ["correction"] if decision == "current" else ["request", "correction"]
    )
    assert any("应该检查 2.0" in prompt for prompt in server.prompts)
    assert ("上一轮答复尚未发布" in server.prompts[-1]) is (decision == "current")


def test_followup_snapshot_read_blocks_stale_reply_until_classified(
    tmp_path: Path,
) -> None:
    class ReadbackServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.original_returned = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                return FakeServer.run_turn(self, prompt, **kwargs)
            result = super().run_turn(prompt, **kwargs)
            self.original_returned.set()
            return result

        def steer(
            self, _thread_id: str, prompt: str, *, expected_turn_id: str | None = None
        ) -> None:
            assert prompt
            raise TurnNotActive("turn already finished")

    service, feishu, _ = _service(tmp_path)
    server = ReadbackServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread", text="检查 1.0")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    classifier: threading.Thread | None = None
    reading, allow_read = threading.Event(), threading.Event()
    try:
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
            text="应该检查 2.0",
        )
        assert service.process_next()

        def block_read(message_id: str) -> None:
            if message_id == "correction":
                reading.set()
                assert allow_read.wait(timeout=5)

        feishu.on_get = block_read
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert reading.wait(timeout=3)
        server.release.set()
        assert server.original_returned.wait(timeout=3)
        worker.join(timeout=0.5)
    finally:
        allow_read.set()
        if classifier is not None:
            classifier.join(timeout=5)
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        deliveries = conn.execute(
            """SELECT e.message_id FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id ORDER BY d.id"""
        ).fetchall()
    assert [row[0] for row in deliveries] == ["correction"]


def test_claimed_followup_blocks_reply_before_the_classifier_takes_the_lock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class ClaimServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.original_returned = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                return FakeServer.run_turn(self, prompt, **kwargs)
            result = super().run_turn(prompt, **kwargs)
            self.original_returned.set()
            return result

    service, feishu, _ = _service(tmp_path)
    server = ClaimServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    claimed, allow_claim = threading.Event(), threading.Event()
    original_claim = service.events.claim_next_steer

    def delayed_claim(**kwargs: Any) -> Any:
        event = original_claim(**kwargs)
        if event is not None:
            claimed.set()
            assert allow_claim.wait(timeout=5)
        return event

    monkeypatch.setattr(service.events, "claim_next_steer", delayed_claim)
    classifier: threading.Thread | None = None
    try:
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
            text="应该检查 2.0",
        )
        assert service.process_next()
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert claimed.wait(timeout=3)
        server.release.set()
        assert server.original_returned.wait(timeout=3)
        worker.join(timeout=0.5)
    finally:
        allow_claim.set()
        if classifier is not None:
            classifier.join(timeout=5)
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        deliveries = conn.execute(
            """SELECT e.message_id FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id ORDER BY d.id"""
        ).fetchall()
    assert [row[0] for row in deliveries] == ["correction"]


@pytest.mark.parametrize("first", ["publication", "steer"])
def test_publication_and_followup_claim_are_transactionally_ordered(
    tmp_path: Path, first: str
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        with service.store.connect() as conn:
            origin = conn.execute(
                "SELECT id FROM coworker_events WHERE message_id = 'request'"
            ).fetchone()[0]
        if first == "publication":
            assert service.events.try_begin_native_publication(origin)
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="request",
            mention=False,
        )
        assert service.process_next()
        if first == "steer":
            event = service.events.claim_next_steer(
                app_id="app", chat_id=None, native=True
            )
            assert event is not None
            assert not service.events.try_begin_native_publication(origin)
            service.events.finish_steer(event.id, retry=True)
            assert not service.events.try_begin_native_publication(origin)
            service.events.skip_native_steer(event.id)
        assert service.events.try_begin_native_publication(origin)
        assert (
            service.events.claim_next_steer(app_id="app", chat_id=None, native=True)
            is None
        )
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    assert any("followup" in prompt for prompt in server.prompts)


def test_unrelated_request_does_not_release_a_waiting_correction(
    tmp_path: Path,
) -> None:
    class SlowDecisionServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.decisions = ["unrelated", "current"]
            self.classifying = threading.Event()
            self.finish_classifying = threading.Event()
            self.original_returned = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if (
                kwargs["output_model"] is SteerDecision
                and not self.classifying.is_set()
            ):
                self.classifying.set()
                assert self.finish_classifying.wait(timeout=5)
            result = super().run_turn(prompt, **kwargs)
            if kwargs["output_model"] is not SteerDecision:
                self.original_returned.set()
            return result

    service, feishu, _ = _service(tmp_path)
    server = SlowDecisionServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    classifier: threading.Thread | None = None
    correction_id: int | None = None
    try:
        _receive(service, feishu, "unrelated", location="thread", parent="request")
        assert service.process_next()
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert server.classifying.wait(timeout=3)
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
            text="更正：应该检查 2.0",
        )
        assert service.process_next()
        with service.store.connect() as conn:
            correction_id = conn.execute(
                "SELECT id FROM coworker_events WHERE message_id = 'correction'"
            ).fetchone()[0]
        server.release.set()
        assert server.original_returned.wait(timeout=3)
        server.finish_classifying.set()
        classifier.join(timeout=5)
        assert not classifier.is_alive()
        worker.join(timeout=0.5)
        assert worker.is_alive()
        assert not service.process_next_steer()
    finally:
        server.finish_classifying.set()
        server.release.set()
        if classifier is not None:
            classifier.join(timeout=5)
        if correction_id is not None:
            service.events.skip_native_steer(correction_id)
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        deliveries = conn.execute(
            """SELECT e.message_id FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id ORDER BY d.id"""
        ).fetchall()
    assert [row[0] for row in deliveries] == ["unrelated", "correction"]
    assert any("应该检查 2.0" in prompt for prompt in server.prompts)


@pytest.mark.parametrize("blocker", ["handoff", "retry"])
def test_publication_does_not_wait_for_paused_or_deferred_followup(
    tmp_path: Path, blocker: str
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    followup_id: int | None = None
    try:
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="request",
            mention=False,
        )
        assert service.process_next()
        with service.store.connect() as conn:
            followup_id = conn.execute(
                "SELECT id FROM coworker_events WHERE message_id = 'followup'"
            ).fetchone()[0]
            session_id = conn.execute("SELECT id FROM coworker_sessions").fetchone()[0]
            if blocker == "retry":
                conn.execute(
                    """UPDATE coworker_events SET raw_json = json_set(raw_json,
                       '$._runtime.retry_after', '2999-01-01T00:00:00Z') WHERE id = ?""",
                    (followup_id,),
                )
        if blocker == "handoff":
            service.native_state.request_handoff(session_id)
        server.release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
    finally:
        server.release.set()
        if followup_id is not None:
            service.events.skip_native_steer(followup_id)
        worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.parametrize("blocker", ["earlier-retry", "earlier-batch"])
def test_publication_does_not_wait_on_followup_blocked_by_deferred_event(
    tmp_path: Path, blocker: str
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    followup_id: int | None = None
    try:
        _receive(service, feishu, "earlier", location="thread")
        assert service.process_next()
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="request",
            mention=False,
        )
        assert service.process_next()
        with service.store.connect() as conn:
            followup_id = conn.execute(
                "SELECT id FROM coworker_events WHERE message_id = 'followup'"
            ).fetchone()[0]
            if blocker == "earlier-retry":
                conn.execute(
                    """UPDATE coworker_events SET raw_json = json_set(raw_json,
                       '$._runtime.retry_after', '2999-01-01T00:00:00Z') WHERE message_id = 'earlier'"""
                )
            else:
                conn.execute(
                    """UPDATE coworker_events SET raw_json = json_set(raw_json,
                       '$._runtime.batch_leader', 999) WHERE message_id = 'earlier'"""
                )
        server.release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
    finally:
        server.release.set()
        if followup_id is not None:
            service.events.skip_native_steer(followup_id)
        worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.parametrize("reason", ["stop", "server-unhealthy", "runtime-unhealthy"])
def test_worker_stop_or_health_change_releases_waiting_followup(
    tmp_path: Path, monkeypatch: Any, reason: str
) -> None:
    class WorkerServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.healthy = True

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def is_healthy(self) -> bool:
            return self.healthy

    service, feishu, _ = _service(tmp_path)
    server = WorkerServer()
    service.app_server = server  # type: ignore[assignment]
    monkeypatch.setattr(service, "process_next_steer", lambda: False)
    waiting = threading.Event()
    begin_publication = service.events.try_begin_native_publication

    def observe_wait(event_id: int, **kwargs: Any) -> bool:
        ready = begin_publication(event_id, **kwargs)
        if not ready:
            waiting.set()
        return ready

    monkeypatch.setattr(service.events, "try_begin_native_publication", observe_wait)
    runtime_healthy = threading.Event()
    runtime_healthy.set()
    worker = NativeCoworkerWorker(
        service,
        runtime_allows_processing=runtime_healthy.is_set,
        wake_daemon=lambda: None,
    )
    worker.start()
    stopper: threading.Thread | None = None
    followup_id: int | None = None
    try:
        _receive(service, feishu, "request", location="thread")
        assert server.started.wait(timeout=3)
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            parent="request",
            mention=False,
        )
        for _ in range(60):
            with service.store.connect() as conn:
                row = conn.execute(
                    "SELECT id, topic_key FROM coworker_events WHERE message_id = 'followup'"
                ).fetchone()
            if row is not None and row["topic_key"] == "thread":
                followup_id = int(row["id"])
                break
            threading.Event().wait(timeout=0.05)
        assert followup_id is not None
        server.release.set()
        assert waiting.wait(timeout=3)
        if reason == "stop":
            stopper = threading.Thread(target=worker.stop)
            stopper.start()
            stopper.join(timeout=4)
            assert not stopper.is_alive()
        else:
            if reason == "server-unhealthy":
                server.healthy = False
            else:
                runtime_healthy.clear()
            status = "processing"
            for _ in range(80):
                with service.store.connect() as conn:
                    status = conn.execute(
                        "SELECT status FROM coworker_events WHERE message_id = 'request'"
                    ).fetchone()[0]
                if status != "processing":
                    break
                threading.Event().wait(timeout=0.05)
            assert status == "completed"
    finally:
        server.release.set()
        if stopper is not None and stopper.is_alive() and followup_id is not None:
            service.events.skip_native_steer(followup_id)
            stopper.join(timeout=5)
        elif stopper is None:
            worker.stop()


@pytest.mark.parametrize("change", ["delete", "edit"])
def test_changed_late_followup_does_not_suppress_the_original_reply(
    tmp_path: Path, change: str
) -> None:
    class ClassificationServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.classifying = threading.Event()
            self.finish_classifying = threading.Event()
            self.original_returned = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                self.classifying.set()
                assert self.finish_classifying.wait(timeout=5)
                return FakeServer.run_turn(self, prompt, **kwargs)
            result = super().run_turn(prompt, **kwargs)
            self.original_returned.set()
            return result

    service, feishu, _ = _service(tmp_path)
    server = ClassificationServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    classifier: threading.Thread | None = None
    try:
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
            text="应该检查 2.0",
        )
        assert service.process_next()
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert server.classifying.wait(timeout=3)
        server.release.set()
        assert server.original_returned.wait(timeout=3)
        if change == "delete":
            feishu.messages["correction"]["is_deleted"] = True
        else:
            feishu.messages["correction"]["content"] = "应该检查 3.0"
    finally:
        server.finish_classifying.set()
        if classifier is not None:
            classifier.join(timeout=5)
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    while service.process_next():
        pass
    with service.store.connect() as conn:
        deliveries = conn.execute(
            """SELECT e.message_id FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id ORDER BY d.id"""
        ).fetchall()
        status = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id = 'correction'"
        ).fetchone()[0]
    assert deliveries[0][0] == "request"
    assert status == ("needs_review" if change == "delete" else "completed")
    if change == "edit":
        assert [row[0] for row in deliveries] == ["request", "correction"]
        assert any("应该检查 3.0" in prompt for prompt in server.prompts)


def test_followup_classifier_uses_isolated_notifications_and_closes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    service, _, _ = _service(tmp_path)
    policy = service.current_policy()
    assert policy is not None
    outcome = "current"
    closed: list[str] = []
    budgets: dict[str, float] = {}

    class IsolatedServer:
        def __init__(self, *, on_notification: Any, **kwargs: Any) -> None:
            assert kwargs["skill_names"] == []
            assert kwargs["explicit_context_paths"] == []
            self.notify = on_notification

        def start(self) -> None:
            self.notify(
                {
                    "method": "turn/started",
                    "params": {"threadId": "classifier", "turn": {"id": "turn"}},
                }
            )
            if outcome == "failure":
                raise AppServerError("start failed")

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            assert kwargs["dry_run_safety"] and 0 < kwargs["turn_timeout_seconds"] <= 30
            assert kwargs["ephemeral"]
            budgets[prompt] = kwargs["turn_timeout_seconds"]
            if outcome == "timeout":
                return AgentRunResult(argv=[], exit_code=None, timed_out=True)
            decision = "current" if prompt == "full context" else outcome
            return AgentRunResult(
                argv=[], exit_code=0, json_data={"decision": decision}
            )

        def close(self) -> None:
            self.notify(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "classifier", "turn": {"id": "turn"}},
                }
            )
            closed.append(outcome)

    monkeypatch.setattr(
        "feishu_shadow_agent.coworker_native.CodexAppServer", IsolatedServer
    )
    for _ in range(260):
        assert (
            NativeCoworkerService._classify_followup(  # pyright: ignore[reportPrivateUsage]
                service, policy, "classify", str(tmp_path)
            )
            == "current"
        )
    for outcome in ("failure", "timeout", "invalid"):
        assert (
            NativeCoworkerService._classify_followup(  # pyright: ignore[reportPrivateUsage]
                service, policy, "classify", str(tmp_path)
            )
            == "uncertain"
        )
        assert closed[-1] == outcome
    assert len(closed) == 263
    outcome = "uncertain"
    clock = iter((10.0, 18.0))
    with monkeypatch.context() as mocked:
        mocked.setattr(
            "feishu_shadow_agent.coworker_native.time",
            SimpleNamespace(monotonic=clock.__next__),
        )
        assert (
            NativeCoworkerService._classify_followup(  # pyright: ignore[reportPrivateUsage]
                service,
                policy,
                "classify",
                str(tmp_path),
                fallback_prompt=lambda: "full context",
            )
            == "current"
        )
    assert budgets == {"classify": 30, "full context": 22}
    assert len(closed) == 264
    assert not service._child_turns  # pyright: ignore[reportPrivateUsage]


def test_slow_classification_does_not_block_another_topic(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class WorkerServer(FakeServer):
        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def is_healthy(self) -> bool:
            return True

    service, _, _ = _service(tmp_path)
    service.app_server = WorkerServer()  # type: ignore[assignment]
    pending: queue.Queue[str] = queue.Queue()
    first_started, release_first, second_finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    pending.put("first-topic")
    pending.put("second-topic")

    def steer() -> bool:
        try:
            topic = pending.get_nowait()
        except queue.Empty:
            return False
        if topic == "first-topic":
            first_started.set()
            assert release_first.wait(timeout=5)
        else:
            second_finished.set()
        return True

    monkeypatch.setattr(service, "process_next", lambda **_kwargs: False)
    monkeypatch.setattr(service, "process_next_steer", steer)
    monkeypatch.setattr(service, "maintain_memory_if_due", lambda: False)
    worker = NativeCoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )
    worker.start()
    try:
        assert first_started.wait(timeout=3)
        assert second_finished.wait(timeout=2)
    finally:
        release_first.set()
        worker.stop()


def test_locator_only_queues_message_without_starting_a_model(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    _receive(service, feishu, "request", location="thread")
    assert service.process_next(locating_only=True)
    assert not server.prompts
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT status, topic_key FROM coworker_events WHERE message_id = 'request'"
        ).fetchone()
    assert tuple(row) == ("queued", "thread")
    assert service.process_next() and len(server.prompts) == 1


def test_ready_followup_is_steered_before_a_slow_unrelated_location(
    tmp_path: Path,
) -> None:
    class SignalingServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.steered = threading.Event()

        def steer(self, *args: Any, **kwargs: Any) -> None:
            super().steer(*args, **kwargs)
            self.steered.set()

    service, feishu, _ = _service(tmp_path)
    server = SignalingServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    task = threading.Thread(target=service.process_next)
    task.start()
    assert server.started.wait(timeout=3)
    locating, release_location = threading.Event(), threading.Event()
    steering: threading.Thread | None = None
    try:
        _receive(
            service,
            feishu,
            "correction",
            location="thread",
            parent="request",
            mention=False,
        )
        assert service.process_next(locating_only=True)
        _receive(service, feishu, "unrelated", location="thread", thread="other")

        def block_unrelated(message_id: str) -> None:
            if message_id == "unrelated":
                locating.set()
                assert release_location.wait(timeout=5)

        feishu.on_get = block_unrelated
        worker = NativeCoworkerWorker(
            service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
        )
        steering = threading.Thread(target=worker._process_steering)
        steering.start()
        assert locating.wait(timeout=3)
        assert server.steered.is_set()
        assert "correction" in server.steers[0]
    finally:
        release_location.set()
        server.release.set()
        if steering is not None:
            steering.join(timeout=5)
        task.join(timeout=5)
    assert steering is not None and not steering.is_alive()
    assert not task.is_alive()


@pytest.mark.parametrize("topic_count", [1, 2])
def test_followup_is_steered_when_all_topic_workers_are_busy(
    tmp_path: Path, topic_count: int
) -> None:
    class SaturatedServer(FakeServer):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.started = threading.Event()
            self.release = threading.Event()
            self.steered = threading.Event()
            self.steers: list[str] = []
            self.count = 0

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def is_healthy(self) -> bool:
            return True

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                return super().run_turn(prompt, **kwargs)
            with self.lock:
                self.count += 1
                thread_id = f"active-{self.count}"
                self.prompts.append(prompt)
                if self.count == topic_count:
                    self.started.set()
            kwargs["on_thread"](thread_id)
            assert self.release.wait(timeout=8)
            return AgentRunResult(
                argv=[],
                exit_code=0,
                session_id=thread_id,
                json_data={"action": "reply", "text": "answer"},
            )

        def active_turn_id(self, thread_id: str) -> str | None:
            return None if self.release.is_set() else f"{thread_id}-turn"

        def steer(
            self, thread_id: str, prompt: str, *, expected_turn_id: str | None = None
        ) -> None:
            assert expected_turn_id == f"{thread_id}-turn"
            self.steers.append(prompt)
            self.steered.set()

    service, feishu, _ = _service(tmp_path)
    service.config.coworker.max_concurrent_topics = topic_count
    service.store.import_product_policy_from_config(service.config, replace=True)
    server = SaturatedServer()
    service.app_server = server  # type: ignore[assignment]
    worker = NativeCoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )
    worker.start()
    try:
        for index in range(topic_count):
            _receive(
                service,
                feishu,
                f"request-{index}",
                location="thread",
                thread=f"topic-{index}",
                root=f"root-{index}",
            )
        assert server.started.wait(timeout=5)
        _receive(
            service,
            feishu,
            "followup",
            location="thread",
            thread="topic-0",
            root="root-0",
            parent="request-0",
            mention=False,
            text="补充：截止周五",
        )
        assert server.steered.wait(timeout=5)
        assert "截止周五" in server.steers[0]
    finally:
        server.release.set()
        worker.stop()


def test_supplement_classification_stays_ordered_within_one_topic(
    tmp_path: Path,
) -> None:
    class DelayedServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.classifying = threading.Event()
            self.continue_classification = threading.Event()
            self.delay_once = True

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision and self.delay_once:
                self.delay_once = False
                self.classifying.set()
                assert self.continue_classification.wait(timeout=5)
            return super().run_turn(prompt, **kwargs)

    service, feishu, _ = _service(tmp_path)
    server = DelayedServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    classifier: threading.Thread | None = None
    try:
        for message_id in ("first", "second"):
            _receive(
                service,
                feishu,
                message_id,
                location="thread",
                parent="request",
                mention=False,
            )
            assert service.process_next()
        classifier = threading.Thread(target=service.process_next_steer)
        classifier.start()
        assert server.classifying.wait(timeout=3)
        assert not service.process_next_steer()
        server.continue_classification.set()
        classifier.join(timeout=3)
        assert not classifier.is_alive()
        assert service.process_next_steer()
        assert "first" in server.steers[0] and "second" in server.steers[1]
    finally:
        server.continue_classification.set()
        server.release.set()
        if classifier is not None:
            classifier.join(timeout=5)
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_uncertain_steer_blocks_both_outbox_and_approval(tmp_path: Path) -> None:
    for approval in (False, True):
        case = tmp_path / str(approval)
        case.mkdir()
        service, feishu, _ = _service(case)
        server = BlockingNativeServer(fail_steer=True, approval=approval)
        service.app_server = server  # type: ignore[assignment]
        _receive(service, feishu, "one", location="thread")
        worker = threading.Thread(target=service.process_next)
        worker.start()
        assert server.started.wait(timeout=3)
        try:
            _receive(
                service,
                feishu,
                "supplement",
                location="thread",
                parent="one",
                text="补充：截止周五",
            )
            assert service.process_next()
            assert not service.process_next_steer()
        finally:
            server.release.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        with service.store.connect() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM coworker_events WHERE message_id='one'"
                ).fetchone()[0]
                == "needs_review"
            )
            assert (
                conn.execute("SELECT status FROM coworker_native_steers").fetchone()[0]
                == "needs_review"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM coworker_native_deliveries"
                ).fetchone()[0]
                == 0
            )
            assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM coworker_events WHERE message_id IN ('one', 'supplement')"
                )
            ]
        assert not feishu.replies
        for event_id in ids:
            service.events.discard_event(event_id, daemon_stopped=True)
        assert service.events.status()["native_steers"][0]["status"] == "discarded"


@pytest.mark.parametrize("recovers", [True, False])
def test_steer_receipt_write_failure_does_not_resend_or_block_worker_stop(
    tmp_path: Path, recovers: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, feishu, _ = _service(tmp_path)
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    record = service.native_state.record_steer
    writes = 0

    def write_receipt(**kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1 or not recovers:
            raise sqlite3.OperationalError("receipt store unavailable")
        record(**kwargs)

    monkeypatch.setattr(service.native_state, "record_steer", write_receipt)
    _receive(service, feishu, "origin", location="thread")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(service, feishu, "followup", location="thread", parent="origin")
        assert service.process_next()
        if recovers:
            assert service.process_next_steer()
        else:
            with pytest.raises(sqlite3.OperationalError):
                service.process_next_steer()
            service.wait_for_queued_followups = lambda: False
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert writes == 2 and len(server.steers) == 1
    with service.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id='origin'"
        ).fetchone()[0]
    assert status == ("completed" if recovers else "needs_review")
    if recovers:
        assert service.process_next()
        assert len(feishu.replies) == 1
    else:
        assert not feishu.replies


@pytest.mark.parametrize("edit_before_turn", [False, True])
def test_watch_batch_excludes_followup_steered_during_turn(
    tmp_path: Path, edit_before_turn: bool
) -> None:
    service, feishu, first_server = _service(tmp_path)
    first_server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "update-one", mention=False, location="thread")
    _receive(
        service,
        feishu,
        "update-two",
        mention=False,
        location="thread",
        parent="update-one",
        text="普通更新" if edit_before_turn else "补充：截止周五",
    )
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET received_at='2000-01-01T00:00:00Z',
                     raw_json=json_remove(raw_json, '$._runtime.retry_after') WHERE status='queued'""")
    if edit_before_turn:
        feishu.messages["update-two"]["content"] = "补充：截止周五"
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        assert "update-two" not in server.prompts[0]
        assert service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    with service.store.connect() as conn:
        states = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id IN ('update-one', 'update-two') ORDER BY id"
        ).fetchall()
        assert [row[0] for row in states] == ["completed", "completed"]


def test_reserved_watch_batch_cannot_be_steered_after_edit(tmp_path: Path) -> None:
    service, feishu, first_server = _service(tmp_path)
    first_server.outputs.append({"action": "watch", "text": "watching"})
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
    server = BlockingNativeServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "update-one", mention=False, location="thread")
    _receive(
        service,
        feishu,
        "update-two",
        mention=False,
        location="thread",
        parent="update-one",
        text="普通更新",
    )
    assert service.process_next() and service.process_next()
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET received_at='2000-01-01T00:00:00Z',
                     raw_json=json_remove(raw_json, '$._runtime.retry_after') WHERE status='queued'""")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        assert "update-two" in server.prompts[0]
        feishu.messages["update-two"]["content"] = "补充：截止周五"
        assert not service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            == "needs_review"
        )


def test_transfer_waits_for_topic_receipt_then_starts_separate_session(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request", location="main")
    assert service.process_next()
    assert len(server.prompts) == 1 and not feishu.replies
    assert service.process_next()
    assert len(server.prompts) == 1 and feishu.sent == 1
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None]
    assert server.output_models == [NativeMainOutput, NativeReplyOutput]
    assert "集中提出必要的澄清问题" in server.prompts[-1]
    assert "写入本群专用记忆" in server.prompts[-1]
    assert all("禁止用工具发送或回复飞书消息" in prompt for prompt in server.prompts)
    assert feishu.replies[-1]["message_id"] == "sent-1"
    assert feishu.replies[-1]["reply_in_thread"] is True
    with service.store.connect() as conn:
        rows = conn.execute(
            "SELECT purpose, status, thread_id FROM coworker_native_deliveries ORDER BY id"
        ).fetchall()
        assert tuple(rows[0]) == ("transfer", "sent", "opened-thread")
        assert tuple(rows[1]) == ("reply", "sent", None)

    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-2",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "topic-child",
                },
            },
        }
    )
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "topic-child",
                    "turn": {
                        "id": "child-turn",
                        "status": "completed" if method == "turn/completed" else None,
                    },
                },
            }
        )
    assert service.process_next() and service.process_next()
    assert feishu.replies[-1]["message_id"] == "sent-1"


def test_transfer_approval_uses_confirmed_topic_and_keeps_pending_acknowledgement(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.extend(
        [
            {"action": "transfer", "text": "acknowledged task"},
            {"action": "reply", "text": "draft", "approval_required": True},
        ]
    )
    _receive(service, feishu, "request")
    assert service.process_next() and service.process_next()
    assert feishu.sent == 1
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE coworker_native_deliveries SET created_at='2000-01-01T00:00:00Z'"
        )
    retention = RetentionService(
        store=service.store, config=service.config, base_dir=tmp_path
    )
    retention.prune(protect_pending=True)
    assert service.process_next()
    assert "acknowledged task" in server.prompts[-1]
    assert feishu.sent == 1
    result = _dispatch_manual_reply(service, feishu)
    assert result.sent == 1 and feishu.sent == 2
    assert feishu.replies[-1]["message_id"] == "sent-1"
    assert feishu.replies[-1]["reply_in_thread"] is True
    retention.prune(protect_pending=True)
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT text FROM coworker_native_deliveries WHERE purpose='transfer'"
            ).fetchone()[0]
            == ""
        )


@pytest.mark.parametrize("location", ["topic", "main", "other"])
@pytest.mark.parametrize("followup", ["single", "chain", "ignored", "cross"])
def test_quoted_followup_selects_matching_pending_origin(
    tmp_path: Path, location: str, followup: str
) -> None:
    class ParallelServer(FakeServer):
        def __init__(self) -> None:
            super().__init__()
            self.topic_started = threading.Event()
            self.release_topic = threading.Event()

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if "飞书话题已经建立" in kwargs.get("instructions", ""):
                self.topic_started.set()
                assert self.release_topic.wait(timeout=5)
            return super().run_turn(prompt, **kwargs)

    service, feishu, _ = _service(tmp_path, dry_run=False)
    server = ParallelServer()
    service.app_server = server  # type: ignore[assignment]
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "main-child",
                },
            },
        }
    )
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "main-child",
                    "turn": {"id": "child-turn", "status": "completed"},
                },
            }
        )
    assert service.process_next()  # Fake client confirms the topic-opening reply.
    topic_task = threading.Thread(target=service.process_next)
    topic_task.start()
    assert server.topic_started.wait(timeout=3)
    try:
        assert service.process_next(internal_only=True)
    finally:
        server.release_topic.set()
        topic_task.join(timeout=5)
    assert not topic_task.is_alive()
    with service.store.connect() as conn:
        pending = conn.execute(
            """SELECT e.id, e.topic_key FROM coworker_native_deliveries d
               JOIN coworker_events e ON e.id = d.event_id
               WHERE d.status = 'pending' ORDER BY e.id"""
        ).fetchall()
    assert [row["topic_key"] for row in pending] == ["__group_main__", "opened-thread"]

    _receive(
        service,
        feishu,
        "correction",
        mention=False,
        parent="request",
        location="main" if location == "main" else "thread",
        thread="opened-thread" if location == "topic" else "other",
        root="request",
        text="@_all 通知" if followup == "ignored" else "请改为检查 2.0",
    )
    if followup != "single":
        second_location = location
        if followup == "cross":
            second_location = "main" if location == "topic" else "topic"
        _receive(
            service,
            feishu,
            "correction-two",
            mention=False,
            parent="correction",
            location="main" if second_location == "main" else "thread",
            thread="opened-thread" if second_location == "topic" else "other",
            root="request",
            text="收到谢谢" if followup == "ignored" else "再改为检查 3.0",
        )
    model_calls = len(server.prompts)
    assert (
        service.process_next() and service.process_next()
    )  # Drain both fake deliveries.
    while service.process_next():
        pass
    if location == "other" or followup == "ignored":
        assert len(server.prompts) == model_calls
    else:
        assert len(server.prompts) == model_calls + 1 + int(followup == "chain")
        assert "请改为检查 2.0" in server.prompts[model_calls]
        if followup == "chain":
            assert "再改为检查 3.0" in server.prompts[-1]
        assert all(
            session == ("session-1" if location == "main" else "session-3")
            for session in server.sessions[model_calls:]
        )


def test_followup_candidates_include_origin_that_starts_after_ingress(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "item": {
                    "type": "subAgentActivity",
                    "kind": "started",
                    "agentThreadId": "main-child",
                },
            },
        }
    )
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "main-child",
                    "turn": {"id": "child-turn", "status": "completed"},
                },
            }
        )
    assert service.process_next() and service.process_next(internal_only=True)
    _receive(
        service,
        feishu,
        "correction",
        mention=False,
        parent="request",
        location="thread",
        thread="opened-thread",
        text="请改为检查 2.0",
    )
    with service.store.connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT raw_json FROM coworker_events WHERE message_id = 'correction'"
            ).fetchone()[0]
        )
    assert len(payload["_runtime"]["followup_candidates"]) == 1

    assert service.process_next()  # Send the main child result through the fake client.
    assert service.process_next()  # Start and finish the transferred topic task.
    with service.store.connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT raw_json FROM coworker_events WHERE message_id = 'correction'"
            ).fetchone()[0]
        )
    assert len(payload["_runtime"]["followup_candidates"]) == 2
    while service.process_next():
        pass
    assert "请改为检查 2.0" in server.prompts[-1]
    assert server.sessions[-1] == "session-3"


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "before-start",
        "before-start-mention",
        "before-reply",
        "before-child",
        "retained-transfer-context",
        "retained-source",
        "legacy-v1-running",
        "legacy-v1-queued",
        "legacy-v2-running",
        "legacy-v2-queued",
    ],
)
def test_transfer_preserves_accepted_supplement_and_checks_its_revision(
    tmp_path: Path, change: str
) -> None:
    class TransferServer(BlockingNativeServer):
        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if not self.started.is_set():
                result = super().run_turn(prompt, **kwargs)
                return replace(
                    result, json_data={"action": "transfer", "text": "已接单"}
                )
            return FakeServer.run_turn(self, prompt, **kwargs)

    service, feishu, _ = _service(tmp_path, dry_run=False)
    server = TransferServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="main")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        _receive(
            service,
            feishu,
            "supplement",
            location="main",
            parent="request",
            text="补充：只检查 2.0",
        )
        if change == "before-start-mention":
            feishu.messages["supplement"]["mentions"] = [
                {"id": "u1", "key": "@_user_1", "name": "同事"},
                {"id": "u2", "key": "@_user_2", "name": "同事"},
            ]
        assert service.process_next() and service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert service.process_next()  # The fake client confirms the topic receipt.
    assert feishu.sent == 1
    if change.startswith("legacy-"):
        old = message_content_fingerprint(
            feishu.messages["supplement"],
            include_mentions=change.startswith("legacy-v2"),
            mention_version=2,
        )
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE coworker_native_delivery_context SET snapshot_json = NULL WHERE message_id = 'supplement'"
            )
            conn.execute("UPDATE coworker_native_steers SET snapshot_json = NULL")
            conn.execute(
                """UPDATE messages SET raw_json=json_set(raw_json,
                   '$.coworker_source_content_sha256', ?), inserted_at='2000-01-01T00:00:00Z'
                   WHERE message_id='supplement'""",
                (old,),
            )
    if change == "before-start":
        feishu.messages["supplement"]["content"] = "补充：检查 3.0"
    elif change == "before-start-mention":
        mentions = feishu.messages["supplement"]["mentions"]
        mentions[0]["id"], mentions[1]["id"] = mentions[1]["id"], mentions[0]["id"]
    elif change == "retained-transfer-context":
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE messages SET inserted_at = '2000-01-01T00:00:00Z' WHERE message_id = 'supplement'"
            )
        RetentionService(
            store=service.store, config=service.config, base_dir=tmp_path
        ).prune(protect_pending=True)
    assert service.process_next()
    if change in {"before-start", "before-start-mention"}:
        assert len(server.prompts) == 1
        with service.store.connect() as conn:
            status = conn.execute(
                "SELECT status FROM coworker_events WHERE message_id LIKE 'internal:transfer:%'"
            ).fetchone()[0]
        assert status == "needs_review"
        return

    assert len(server.prompts) == 2
    assert "只检查 2.0" in server.prompts[1] and "已接单" in server.prompts[1]
    if change == "before-reply":
        with service.store.connect() as conn:
            conn.execute(
                "UPDATE messages SET revision = revision + 1 WHERE message_id = 'supplement'"
            )
    assert service.process_next()
    with service.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM coworker_native_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        topic_thread = conn.execute(
            "SELECT backend_session_id FROM coworker_sessions WHERE thread_id = 'opened-thread'"
        ).fetchone()[0]
    assert status == ("needs_review" if change == "before-reply" else "sent")
    assert feishu.sent == (1 if change == "before-reply" else 2)
    if change == "retained-transfer-context":
        RetentionService(
            store=service.store, config=service.config, base_dir=tmp_path
        ).prune(protect_pending=True)
        with service.store.connect() as conn:
            assert not conn.execute(
                "SELECT text FROM messages WHERE message_id = 'supplement'"
            ).fetchone()[0]
    if change in {"before-child", "retained-source"} or change.startswith("legacy-"):
        service.on_app_server_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": topic_thread,
                    "item": {
                        "type": "subAgentActivity",
                        "kind": "started",
                        "agentThreadId": "topic-child",
                    },
                },
            }
        )
        if change == "before-child":
            feishu.messages["supplement"]["content"] = "补充：检查 3.0"
        if change.endswith("-running"):
            RetentionService(
                store=service.store, config=service.config, base_dir=tmp_path
            ).prune(protect_pending=True)
        for method in ("turn/started", "turn/completed"):
            service.on_app_server_notification(
                {
                    "method": method,
                    "params": {
                        "threadId": "topic-child",
                        "turn": {"id": "child-turn", "status": "completed"},
                    },
                }
            )
        if change == "retained-source" or change.endswith("-queued"):
            with service.store.connect() as conn:
                if change == "retained-source":
                    conn.execute(
                        "UPDATE messages SET inserted_at = '2000-01-01T00:00:00Z' WHERE message_id = 'request'"
                    )
            RetentionService(
                store=service.store, config=service.config, base_dir=tmp_path
            ).prune(protect_pending=True)
        model_calls = len(server.prompts)
        assert service.process_next()
        if change != "before-child":
            assert len(server.prompts) == model_calls + 1
            assert service.process_next() and feishu.sent == 3
            RetentionService(
                store=service.store, config=service.config, base_dir=tmp_path
            ).prune(protect_pending=True)
            message_id = "request" if change == "retained-source" else "supplement"
            with service.store.connect() as conn:
                assert not conn.execute(
                    "SELECT text FROM messages WHERE message_id = ?", (message_id,)
                ).fetchone()[0]
            return
        assert len(server.prompts) == model_calls and feishu.sent == 2
        with service.store.connect() as conn:
            state = conn.execute(
                "SELECT status FROM coworker_events WHERE message_id LIKE 'internal:subagent:%'"
            ).fetchone()[0]
        assert state == "needs_review"


@pytest.mark.parametrize(
    "edit_supplement", ["none", "before-completion", "before-reply"]
)
def test_child_reply_inherits_supplement_accepted_inside_new_topic(
    tmp_path: Path, edit_supplement: str
) -> None:
    class TopicServer(BlockingNativeServer):
        def __init__(self) -> None:
            super().__init__()
            self.first = True

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            if kwargs["output_model"] is SteerDecision:
                return FakeServer.run_turn(self, prompt, **kwargs)
            if self.first:
                self.first = False
                self.outputs.append({"action": "transfer", "text": "已接单"})
                return FakeServer.run_turn(self, prompt, **kwargs)
            if not self.started.is_set():
                return super().run_turn(prompt, **kwargs)
            return FakeServer.run_turn(self, prompt, **kwargs)

    service, feishu, _ = _service(tmp_path, dry_run=False)
    server = TopicServer()
    service.app_server = server  # type: ignore[assignment]
    _receive(service, feishu, "request", location="main")
    assert service.process_next() and service.process_next()
    worker = threading.Thread(target=service.process_next)
    worker.start()
    assert server.started.wait(timeout=3)
    try:
        service.on_app_server_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "active-session",
                    "item": {
                        "type": "subAgentActivity",
                        "kind": "started",
                        "agentThreadId": "topic-child",
                    },
                },
            }
        )
        _receive(
            service,
            feishu,
            "supplement",
            location="thread",
            thread="opened-thread",
            parent="sent-1",
            text="补充：只检查 2.0",
        )
        assert service.process_next() and service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert service.process_next() and feishu.sent == 2
    if edit_supplement == "before-completion":
        feishu.messages["supplement"]["content"] = "补充：只检查 3.0"
    for method in ("turn/started", "turn/completed"):
        service.on_app_server_notification(
            {
                "method": method,
                "params": {
                    "threadId": "topic-child",
                    "turn": {"id": "child-turn", "status": "completed"},
                },
            }
        )
    model_calls = len(server.prompts)
    assert service.process_next()
    if edit_supplement == "before-completion":
        assert len(server.prompts) == model_calls and feishu.sent == 2
        with service.store.connect() as conn:
            status = conn.execute(
                "SELECT status FROM coworker_events WHERE message_id LIKE 'internal:subagent:%'"
            ).fetchone()[0]
        assert status == "needs_review"
        return
    assert len(server.prompts) == model_calls + 1
    if edit_supplement == "before-reply":
        feishu.messages["supplement"]["content"] = "补充：只检查 3.0"
    assert service.process_next()
    with service.store.connect() as conn:
        delivery = conn.execute(
            "SELECT id, status FROM coworker_native_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        context = conn.execute(
            "SELECT message_id FROM coworker_native_delivery_context WHERE delivery_id = ?",
            (delivery["id"],),
        ).fetchall()
    assert [item[0] for item in context] == ["supplement"]
    assert delivery["status"] == (
        "needs_review" if edit_supplement == "before-reply" else "sent"
    )
    assert feishu.sent == (2 if edit_supplement == "before-reply" else 3)


def test_transfer_dry_run_never_claims_topic_or_starts_task(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next() and service.process_next()
    assert not service.process_next()
    assert len(server.prompts) == 1 and all(item["dry_run"] for item in feishu.replies)
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_native_deliveries").fetchone()[0]
            == "previewed"
        )
        assert (
            conn.execute("SELECT count(*) FROM coworker_native_transfers").fetchone()[0]
            == 0
        )


def test_topic_mode_result_can_be_quoted_in_opened_topic(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    service.reply_mode = "topic"
    _receive(service, feishu, "request")
    assert service.process_next() and service.process_next()
    _receive(
        service,
        feishu,
        "follow",
        mention=False,
        parent="sent-1",
        location="thread",
        thread="opened-thread",
        root="request",
    )
    assert service.process_next() and service.process_next()
    assert len(server.prompts) == 2 and server.sessions == [None, None]


def test_main_position_on_topic_receipt_never_starts_transfer(tmp_path: Path) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    feishu.receipt_in_main = True
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next() and service.process_next()
    assert not service.process_next() and len(server.prompts) == 1
    with service.store.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_native_deliveries").fetchone()[0]
            == "needs_review"
        )


def test_new_topic_followup_waits_for_transfer_even_with_same_timestamp(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next()

    def incoming() -> None:
        feishu.on_actual = None
        _receive(
            service,
            feishu,
            "fast-followup",
            location="thread",
            thread="opened-thread",
            root="new-root",
        )
        assert service.process_next()
        assert len(server.prompts) == 1

    feishu.on_actual = incoming
    assert service.process_next()
    assert service.process_next()
    assert len(server.prompts) == 2 and "话题已经建立" in server.prompts[1]


def test_missing_topic_receipt_needs_review_without_repeating_model_or_send(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    feishu.omit_thread = True
    server.outputs.append({"action": "transfer", "text": "已接单"})
    _receive(service, feishu, "request")
    assert service.process_next() and service.process_next()
    assert not service.process_next()
    assert len(server.prompts) == 1 and feishu.sent == 1
    with service.store.connect() as conn:
        row = conn.execute(
            "SELECT id, status, sent_message_id FROM coworker_native_deliveries"
        ).fetchone()
        assert tuple(row)[1:] == ("needs_review", "sent-1")
        delivery_id = row[0]
    _receive(service, feishu, "unrelated", location="thread", thread="unrelated-thread")
    assert service.process_next() and service.process_next()
    assert feishu.replies[-1]["message_id"] == "unrelated"
    service.deliveries.resolve_uncertain(
        delivery_id,
        sent_message_id="sent-1",
        thread_id="verified-thread",
        daemon_stopped=True,
    )
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None, None] and feishu.sent == 3


def test_topic_with_lost_reply_receipt_waits_by_actual_bot_anchor(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path, dry_run=False)
    server.outputs.append({"action": "transfer", "text": "acknowledged"})
    _receive(service, feishu, "request")
    assert service.process_next()
    feishu.fail_actual = True
    assert service.process_next()
    feishu.fail_actual = False
    feishu.messages["sent-1"].update(
        sender={"id": "app", "sender_type": "app"}, parent_id="request"
    )
    _receive(
        service,
        feishu,
        "related",
        location="thread",
        parent="sent-1",
        thread="opened-thread",
        root="sent-1",
        mention=False,
    )
    assert service.process_next()
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT sent_message_id FROM coworker_native_deliveries WHERE status='needs_review'"
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id='related'"
            ).fetchone()[0]
            == "queued"
        )
    _receive(
        service,
        feishu,
        "other",
        location="thread",
        thread="other-thread",
        root="other-root",
    )
    assert service.process_next() and service.process_next()
    assert len(server.prompts) == 2 and feishu.replies[-1]["message_id"] == "other"


def test_native_memory_maintenance_disables_shared_native_memory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    service, _feishu, _server = _service(tmp_path, dry_run=False)
    observed: dict[str, Any] = {}

    class MemoryServer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            observed["started"] = True

        def close(self) -> None:
            observed["closed"] = True

        def run_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            observed.update(kwargs)
            observed["prompt"] = prompt
            return AgentRunResult(argv=[], exit_code=0)

    monkeypatch.setattr(
        "feishu_shadow_agent.coworker_native.CodexAppServer", MemoryServer
    )
    policy = service.current_policy()
    assert policy is not None
    backend = service.backend_factory(service.config, policy, service.base_dir)
    assert service.run_memory_task(backend, "整理", tmp_path / "workspace").ok
    assert observed["private"] is True and observed["started"] and observed["closed"]
    assert "只整理本群资料" in observed["instructions"]
