from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    CoworkerConfig,
    OwnerConfig,
)
from feishu_shadow_agent.coworker_native import NativeCoworkerService
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.operator_commands import OperatorCommandService
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
        self.private: list[bool] = []
        self.outputs: list[dict[str, Any]] = []
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
        private: bool = False,
        unload_delay_seconds: int | None = None,
        dry_run_safety: bool = False,
    ) -> AgentRunResult:
        self.sessions.append(session_id)
        self.prompts.append(prompt)
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
        private: bool = False,
        unload_delay_seconds: int | None = None,
        dry_run_safety: bool = False,
    ) -> AgentRunResult:
        self.prompts.append(prompt)
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
    service = NativeCoworkerService(
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
) -> None:
    source: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": "group",
        "thread_id": thread,
        "content": text or f"request {message_id}",
        "msg_type": "text",
        "message_position" if location == "main" else "thread_message_position": 1,
    }
    feishu.messages[message_id] = source
    message = {
        "message_id": message_id,
        "chat_id": "group",
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
) -> None:
    content = f"forwarded content {message_id}"
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
    assert feishu.replies[-1]["reply_in_thread"] is True
    with service.store.connect() as conn:
        assert [
            row[0]
            for row in conn.execute("SELECT status FROM coworker_native_deliveries")
        ] == ["previewed", "previewed", "previewed"]


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
    assert not feishu.replies
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


def test_policy_revocation_during_source_read_blocks_preview_and_send(
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
    assert feishu.replies == []


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
    assert "internal_subagent_completion" in server.prompts[1]
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


def test_running_child_supplement_uses_new_origin_even_if_it_finishes_during_call(
    tmp_path: Path,
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
                "threadId": "child",
                "turn": {"id": "child-turn", "status": "completed"},
            },
        }
    )
    assert not service.process_next()
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "session-1",
                "turnId": "root-turn-2",
                "item": {**item, "status": "completed"},
            },
        }
    )
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
    assert ".private.dry-run" in server.prompts[0]
    assert all(call["dry_run"] for call in feishu.replies)


def test_private_quote_waits_for_uncertain_receipt_without_exhausting_retries(
    tmp_path: Path,
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
    service.deliveries.resolve_uncertain(
        delivery["id"], sent_message_id="sent-1", daemon_stopped=True
    )
    feishu.fail_actual = False
    with service.store.connect() as conn:
        conn.execute("""UPDATE coworker_events SET raw_json=json_remove(raw_json, '$._runtime.retry_after')
                        WHERE message_id='follow'""")
    assert service.process_next()
    assert server.sessions == [None, "session-1"]


def test_owner_can_watch_real_topic_and_expiry_stops_unmentioned_messages(
    tmp_path: Path,
) -> None:
    service, feishu, server = _service(tmp_path)
    server.outputs.append(
        {"action": "watch", "text": "watching", "watch_duration_seconds": 3600}
    )
    _receive(service, feishu, "watch", location="thread", sender="owner")
    assert service.process_next() and service.process_next()
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
    assert len(feishu.replies) == 1
    with service.store.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_native_deliveries ORDER BY id DESC"
            ).fetchone()[0]
            == "needs_review"
        )


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
        and "codex resume session-1" in status["operator_command"]
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
            private: bool = False,
            unload_delay_seconds: int | None = None,
            dry_run_safety: bool = False,
        ) -> AgentRunResult:
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


@pytest.mark.parametrize("change", ["none", "before-start", "before-reply"])
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
        assert service.process_next() and service.process_next_steer()
    finally:
        server.release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert service.process_next()  # The fake client confirms the topic receipt.
    assert feishu.sent == 1
    if change == "before-start":
        feishu.messages["supplement"]["content"] = "补充：检查 3.0"
    assert service.process_next()
    if change == "before-start":
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
    assert status == ("needs_review" if change == "before-reply" else "sent")
    assert feishu.sent == (1 if change == "before-reply" else 2)


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
    service.deliveries.resolve_uncertain(
        delivery_id,
        sent_message_id="sent-1",
        thread_id="verified-thread",
        daemon_stopped=True,
    )
    assert service.process_next() and service.process_next()
    assert server.sessions == [None, None] and feishu.sent == 2


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
    assert "只整理本群资料" in observed["prompt"]
