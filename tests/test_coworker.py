from __future__ import annotations

import json
import socket
import stat
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import yaml

from feishu_shadow_agent.agent_backend import AgentRunResult
from feishu_shadow_agent.codex_app_server import AppServerError, TurnNotActive
from feishu_shadow_agent.config import (
    AppConfig,
    ChatPolicyConfig,
    CoworkerConfig,
    CoworkerPartnerConfig,
    OwnerConfig,
)
from feishu_shadow_agent.coworker import CoworkerService, CoworkerWorker
from feishu_shadow_agent.coworker_history import read_cached_history
from feishu_shadow_agent.coworker_memory import MemoryEntry, SharedMemory
from feishu_shadow_agent.coworker_replay import (
    CoworkerReplaySnapshot,
    replay_recorded_events,
    snapshot_from_local_store,
)
from feishu_shadow_agent.dispatcher import Dispatcher
from feishu_shadow_agent.jsonl import JSONLLogger
from feishu_shadow_agent.operator_commands import OperatorCommandService
from feishu_shadow_agent.operator_query import OperatorQueryService
from feishu_shadow_agent.retention import RetentionService
from feishu_shadow_agent.store.sqlite_store import SQLiteStore
from feishu_shadow_agent.types import LarkCliResult, MessagePage


def _event(
    message_id: str,
    *,
    sender: str,
    text: str,
    mention: bool,
    sender_type: str = "user",
    parent_id: str = "root",
) -> dict[str, object]:
    return {
        "header": {"event_id": f"ev_{message_id}", "app_id": "app"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": sender_type},
            "message": {
                "message_id": message_id,
                "chat_id": "group",
                "chat_type": "group",
                "thread_id": "thread",
                "root_id": "root",
                "parent_id": parent_id,
                "message_type": "text",
                "create_time": "1794900000000",
                "content": json.dumps({"text": text}),
                "mentions": [{"id": {"open_id": "bot"}, "key": "@_user_1"}]
                if mention
                else [],
            },
        },
    }


class FakeFeishu:
    def __init__(self) -> None:
        self.identities: list[str] = []
        self.edited = False
        self.sent = False
        self.chat_history_calls = 0
        self.chat_history_items: list[dict[str, Any]] | None = None
        self.sources: dict[str, object] = {}
        self.source_mentions: dict[str, list[Any]] = {}

    def capture(self, raw: dict[str, Any]) -> None:
        message = raw["event"]["message"]
        content = json.loads(message["content"])
        self.sources[message["message_id"]] = content.get("text", content)
        self.source_mentions[message["message_id"]] = message.get("mentions", [])

    def get_messages(self, *, as_identity: str, message_ids: list[str]) -> MessagePage:
        self.identities.append(as_identity)
        items: list[dict[str, Any]] = []
        for message_id in message_ids:
            item = {
                "message_id": message_id,
                "chat_id": "group",
                "thread_id": "thread",
                "thread_message_position": 2,
                "msg_type": "text",
                "content": self.sources.get(message_id),
            }
            if self.edited:
                item["update_time"] = "2026-11-18T03:00:00+00:00"
            if self.source_mentions.get(message_id):
                item["mentions"] = self.source_mentions[message_id]
            items.append(item)
        return MessagePage(items=items)

    def reply_message(self, **_kwargs: object) -> LarkCliResult:
        self.sent = True
        return LarkCliResult(argv=[], exit_code=0)

    def list_thread_messages(
        self,
        *,
        thread_id: str,
        page_size: int,
        as_identity: str,
        order: str,
        page_token: str | None = None,
    ) -> MessagePage:
        self.identities.append(as_identity)
        return MessagePage(
            items=[
                {
                    "message_id": "historic",
                    "chat_id": "group",
                    "thread_id": "thread",
                    "text": "截止周五",
                    "sender_id": "colleague",
                    "create_time": "2026-11-18T00:00:00+00:00",
                }
            ]
        )

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start: str | None,
        end: str | None,
        page_size: int,
        as_identity: str,
        order: str,
        page_token: str | None = None,
    ) -> MessagePage:
        self.chat_history_calls += 1
        if self.chat_history_items is not None:
            return MessagePage(items=self.chat_history_items)
        return self.list_thread_messages(
            thread_id=chat_id, page_size=page_size, as_identity=as_identity, order=order
        )


class FakeBackend:
    def __init__(self) -> None:
        self.session_ids: list[str | None] = []
        self.prompts: list[str] = []
        self.outputs: list[dict[str, object]] = []
        self.on_call: Callable[[], None] | None = None

    def structured_task_session(self, prompt, *, output_model, session_id, cwd):
        self.session_ids.append(session_id)
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call()
        return AgentRunResult(
            argv=[],
            exit_code=0,
            session_id="codex-session",
            json_data=self.outputs.pop(0)
            if self.outputs
            else {"action": "reply", "text": "已记录", "partner": None},
        )

    def natural_task_session(self, prompt, *, session_id, cwd):
        self.session_ids.append(session_id)
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call()
        return AgentRunResult(argv=[], exit_code=0, session_id="codex-session")


class FakeCoworkerService(CoworkerService):
    def receive(self, raw: dict[str, Any], bot_open_id: str) -> None:
        cast(FakeFeishu, self.feishu).capture(raw)
        super().receive(raw, bot_open_id)


def test_string_mention_id_triggers_coworker(tmp_path: Path) -> None:
    service, _db, backend, _feishu = _service(tmp_path)
    event = _event("string-mention", sender="alice", text="处理", mention=True)
    message = cast(dict[str, Any], cast(dict[str, Any], event["event"])["message"])
    message["mentions"] = [{"id": "bot", "key": "@_user_1", "name": "机器人"}]
    service.receive(event, "bot")
    assert service.process_next()
    assert len(backend.prompts) == 1
    assert "机器人" in backend.prompts[0]


def _service(
    tmp_path: Path,
    *,
    history_source: Literal["live_on_demand", "local_cache"] = "live_on_demand",
    trigger_mode: Literal["mention", "followups"] = "mention",
    partner_bots: dict[str, str | CoworkerPartnerConfig] | None = None,
    send_mode: Literal["agent_direct", "project"] = "project",
    chat_layout: str = "topic",
) -> tuple[CoworkerService, SQLiteStore, FakeBackend, FakeFeishu]:
    config = AppConfig(
        owner=OwnerConfig(open_id="owner"),
        coworker=CoworkerConfig(
            enabled=True,
            chat_id="group",
            history_source=history_source,
            trigger_mode=trigger_mode,
            partner_bots=partner_bots or {},
            send_mode=send_mode,
            memory_maintenance=True,
        ),  # type: ignore[arg-type]
        chats={
            "group": ChatPolicyConfig(
                auto_reply=True,
                bot_joined=True,
                reply_identity="bot",
                allow_user_fallback=False,
            )
        },
    )
    db = SQLiteStore(tmp_path / "agent.sqlite3")
    db.import_product_policy_from_config(config)
    backend, feishu = FakeBackend(), FakeFeishu()
    service = FakeCoworkerService(
        store=db,
        config=config,
        feishu=feishu,
        logger=JSONLLogger(tmp_path / "agent.jsonl"),
        base_dir=tmp_path,
        app_id="app",
        dry_run=True,
        chat_layout=chat_layout,
        backend_factory=lambda *_args: backend,
    )
    return service, db, backend, feishu


class FakeAppServer:
    def __init__(self, service: CoworkerService) -> None:
        self.service = service
        self.prompts: list[str] = []
        self.session_ids: list[str | None] = []
        self.steers: list[str] = []
        self.run_hook: Callable[[str, str], dict[str, object] | None] | None = None
        self.steer_error: Exception | None = None
        self.active: set[str] = set()
        self.started = False
        self.closed = False
        self.lock = threading.Lock()

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def is_healthy(self) -> bool:
        return not self.closed

    def active_turn_id(self, thread_id: str) -> str | None:
        return "active-turn" if thread_id in self.active else None

    def steer(self, thread_id: str, prompt: str) -> None:
        if self.steer_error is not None:
            raise self.steer_error
        self.steers.append(prompt)

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        pass

    def run_turn(self, prompt, *, session_id, cwd, output_model, on_thread):
        with self.lock:
            self.prompts.append(prompt)
            self.session_ids.append(session_id)
            thread_id = session_id or f"root-{len(self.prompts)}"
            self.active.add(thread_id)
        on_thread(thread_id)
        try:
            output = self.run_hook(prompt, thread_id) if self.run_hook else None
            return AgentRunResult(
                argv=[],
                exit_code=0,
                session_id=thread_id,
                json_data=output if output_model is not None else None,
            )
        finally:
            self.active.remove(thread_id)


def test_app_server_exit_marks_health_and_preserves_queued_events(
    tmp_path: Path,
) -> None:
    service, db, _backend, _feishu = _service(tmp_path)
    server = FakeAppServer(service)
    service.app_server = server  # type: ignore[assignment]
    wakeups: list[bool] = []
    worker = CoworkerWorker(
        service,
        runtime_allows_processing=lambda: True,
        wake_daemon=lambda: wakeups.append(True),
    )
    service.receive(_event("m1", sender="alice", text="处理", mention=True), "bot")
    wakeups.clear()
    server.closed = True
    assert not worker._server_allows_processing()
    assert db.latest_health_check_status("coworker_app_server") == "failed"
    assert (
        OperatorQueryService(db).coworker_status()["backend_health"]["status"]
        == "failed"
    )
    assert wakeups
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM coworker_events WHERE message_id = 'm1'"
            ).fetchone()[0]
            == "queued"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE check_name = 'coworker_app_server'"
            ).fetchone()[0]
            == 1
        )
    assert not worker._server_allows_processing()
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE check_name = 'coworker_app_server'"
            ).fetchone()[0]
            == 1
        )
    db.update_coworker_policy({"memory_maintenance": False}, actor="test")
    server.closed = False
    worker.start()
    try:
        assert db.latest_health_check_status("coworker_app_server") == "ok"
    finally:
        worker.stop()


def _subagent_activity(
    service: CoworkerService, *, root: str, child: str, kind: str
) -> None:
    service.on_app_server_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": root,
                "item": {
                    "type": "subAgentActivity",
                    "kind": kind,
                    "agentThreadId": child,
                    "agentPath": "worker-1",
                },
            },
        }
    )


def _subagent_turn(
    service: CoworkerService, *, child: str, turn: str, status: str | None
) -> None:
    service.on_app_server_notification(
        {
            "method": "turn/completed" if status else "turn/started",
            "params": {"threadId": child, "turn": {"id": turn, "status": status}},
        }
    )


def test_background_subagent_completion_wakes_its_root_once(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        if "internal_subagent_completion" not in prompt:
            _subagent_turn(service, child="child", turn="child-turn", status=None)
            _subagent_activity(service, root=root, child="child", kind="started")
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    assert service.process_next()
    assert service.events.subagent_context("child")["status"] == "running"  # type: ignore[index]
    _subagent_turn(service, child="child", turn="child-turn", status="completed")
    _subagent_activity(service, root="root-1", child="child", kind="completed")
    _subagent_activity(service, root="root-1", child="child", kind="completed")
    assert service.process_next()
    assert not service.process_next()
    assert server.session_ids == [None, "root-1"]
    assert "`internal_subagent_completion`" in server.prompts[1]
    assert "当前消息 ID：`m1`" in server.prompts[1]
    with db.connect() as conn:
        rows = conn.execute("SELECT status FROM coworker_events ORDER BY id").fetchall()
    assert [row["status"] for row in rows] == ["completed", "completed"]


@pytest.mark.parametrize(
    "error", [None, TurnNotActive("finished"), AppServerError("unknown")]
)
def test_followup_steers_running_turn_or_preserves_delivery_uncertainty(
    tmp_path: Path, error: Exception | None
) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)
    started, release = threading.Event(), threading.Event()

    def hold(prompt: str, root: str) -> None:
        started.set()
        assert release.wait(20)
        return None

    server.run_hook = hold
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    thread = threading.Thread(target=service.process_next)
    thread.start()
    try:
        assert started.wait(20)
        service.receive(
            _event("m2", sender="bob", text="只分析不要部署", mention=True), "bot"
        )
        server.steer_error = error
        assert service.process_next_steer() is (not isinstance(error, TurnNotActive))
    finally:
        release.set()
        thread.join(20)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM coworker_events WHERE message_id = 'm2'"
        ).fetchone()
    assert row["status"] == (
        "queued"
        if isinstance(error, TurnNotActive)
        else "needs_review"
        if error is not None
        else "completed"
    )
    if error is None:
        assert len(server.steers) == 1 and "只分析不要部署" in server.steers[0]


def test_completed_child_does_not_delay_steering_active_parent(tmp_path: Path) -> None:
    service, _db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)
    started, release = threading.Event(), threading.Event()

    def hold(prompt: str, root: str) -> None:
        _subagent_activity(service, root=root, child="child", kind="started")
        _subagent_turn(service, child="child", turn="turn-1", status=None)
        started.set()
        assert release.wait(20)
        return None

    server.run_hook = hold
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    worker = threading.Thread(target=service.process_next)
    worker.start()
    try:
        assert started.wait(20)
        _subagent_turn(service, child="child", turn="turn-1", status="completed")
        service.receive(
            _event("m2", sender="bob", text="不要部署", mention=True), "bot"
        )
        assert service.process_next_steer()
        assert len(server.steers) == 1 and "不要部署" in server.steers[0]
    finally:
        release.set()
        worker.join(20)


@pytest.mark.parametrize("edited_message", [None, "m1", "m2"])
def test_project_completion_can_select_same_topic_steered_message(
    tmp_path: Path,
    edited_message: str | None,
) -> None:
    service, db, _backend, feishu = _service(tmp_path)
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def respond(prompt: str, root: str) -> dict[str, object]:
        if "internal_subagent_completion" in prompt:
            return {
                "action": "reply",
                "text": "分析完成",
                "partner": None,
                "reply_target_message_id": "m2",
            }
        if "当前消息 ID：`m1`" in prompt:
            _subagent_activity(service, root=root, child="child", kind="started")
            _subagent_turn(service, child="child", turn="turn-1", status=None)
        return {"action": "silent", "text": "", "partner": None}

    server.run_hook = respond
    for message_id in ("m1", "m2"):
        service.receive(
            _event(message_id, sender="alice", text=message_id, mention=True), "bot"
        )
        assert service.process_next()
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    _subagent_activity(service, root="root-1", child="child", kind="completed")
    assert service.process_next()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, target_message_id, source_message_id FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
    assert row is not None and row["target_message_id"] == "m2"
    assert row["source_message_id"] == "m1"
    action = db.get_action(row["id"])
    assert action is not None
    if edited_message is not None:
        feishu.sources[edited_message] = "changed"
    dispatcher = Dispatcher(
        store=db,
        feishu_client=feishu,  # type: ignore[arg-type]
        config=service.config,
        logger=service.logger,
    )
    before = len(feishu.identities)
    result = dispatcher._call_send(action, dry_run=True)
    assert len(feishu.identities) == before + 1
    assert result.ok is (edited_message is None)
    if edited_message == "m1":
        assert result.error == "coworker_source_changed_or_deleted"
    elif edited_message == "m2":
        assert result.error == "coworker_reply_target_changed"


def test_worker_runs_distinct_topics_concurrently(tmp_path: Path) -> None:
    service, _db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)
    both_started, release = threading.Event(), threading.Event()
    starts: list[str] = []
    lock = threading.Lock()

    def hold(prompt: str, root: str) -> None:
        with lock:
            starts.append(root)
            if len(starts) == 2:
                both_started.set()
        assert release.wait(20)
        return None

    server.run_hook = hold
    first = _event("m1", sender="alice", text="one", mention=True)
    second = _event("m2", sender="bob", text="two", mention=True)
    second_message = cast(
        dict[str, Any], cast(dict[str, Any], second["event"])["message"]
    )
    second_message.update(
        {
            "thread_id": "other-thread",
            "root_id": "other-root",
            "parent_id": "other-root",
        }
    )
    service.receive(first, "bot")
    service.receive(second, "bot")
    worker = CoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )
    worker.start()
    try:
        assert both_started.wait(20)
        assert len(set(starts)) == 2
    finally:
        release.set()
        worker.stop()
    assert server.closed


def test_shutdown_waits_for_child_and_drains_its_completion(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)
    spawned = threading.Event()

    def spawn(prompt: str, root: str) -> None:
        if "internal_subagent_completion" not in prompt:
            _subagent_activity(service, root=root, child="child", kind="started")
            _subagent_turn(service, child="child", turn="turn-1", status=None)
            spawned.set()
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    worker = CoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )
    worker.start()
    assert spawned.wait(20)
    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (worker.stop(), stopped.set()))
    stopper.start()
    try:
        assert not stopped.wait(0.3)
        _subagent_turn(service, child="child", turn="turn-1", status="completed")
        _subagent_activity(service, root="root-1", child="child", kind="completed")
        assert stopped.wait(20)
    finally:
        _subagent_turn(service, child="child", turn="turn-1", status="completed")
        _subagent_activity(service, root="root-1", child="child", kind="completed")
        stopper.join(20)
    assert server.closed
    assert len(server.prompts) == 2
    with db.connect() as conn:
        rows = conn.execute("SELECT status FROM coworker_events ORDER BY id").fetchall()
    assert [row["status"] for row in rows] == ["completed", "completed"]


def test_interrupted_child_is_quarantined_after_restart(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        _subagent_activity(service, root=root, child="child", kind="started")
        _subagent_turn(service, child="child", turn="turn-1", status=None)
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_subagents SET owner_pid = ?, owner_host = ?",
            (2147483647, socket.gethostname()),
        )
    service.events.recover_interrupted(app_id="app", chat_id="group")
    assert service.events.subagent_context("child")["status"] == "needs_review"  # type: ignore[index]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM coworker_events WHERE message_id LIKE 'internal:%'"
        ).fetchone()
    assert (
        row["status"] == "needs_review" and row["error"] == "subagent_outcome_unknown"
    )


def test_nested_child_reports_to_parent_without_waking_root(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        _subagent_activity(service, root=root, child="child", kind="started")
        _subagent_turn(service, child="child", turn="turn-1", status=None)
        _subagent_activity(service, root="child", child="grandchild", kind="started")
        _subagent_turn(service, child="grandchild", turn="turn-2", status=None)
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    assert service.process_next()
    _subagent_turn(service, child="grandchild", turn="turn-2", status="completed")
    with db.connect() as conn:
        wakes = conn.execute(
            "SELECT count(*) FROM coworker_events WHERE message_id LIKE 'internal:%'"
        ).fetchone()[0]
    assert wakes == 0
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    with db.connect() as conn:
        wakes = conn.execute(
            "SELECT count(*) FROM coworker_events WHERE message_id LIKE 'internal:%'"
        ).fetchone()[0]
    assert wakes == 1


def test_shutdown_drain_does_not_overtake_earlier_group_message(tmp_path: Path) -> None:
    service, _db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        if "当前消息 ID：`m1`" in prompt:
            _subagent_activity(service, root=root, child="child", kind="started")
            _subagent_turn(service, child="child", turn="turn-1", status=None)
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="alice", text="不要部署", mention=True), "bot")
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    assert not service.process_next(internal_only=True)
    assert service.process_next()
    assert "不要部署" in server.prompts[1]
    assert service.process_next(internal_only=True)
    assert "internal_subagent_completion" in server.prompts[2]


def test_late_child_completion_cannot_finish_its_new_turn(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        _subagent_activity(service, root=root, child="child", kind="started")
        _subagent_turn(service, child="child", turn="turn-1", status=None)
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="分析", mention=True), "bot")
    assert service.process_next()
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    _subagent_turn(service, child="child", turn="turn-2", status=None)
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    _subagent_activity(service, root="root-1", child="child", kind="completed")
    child = service.events.subagent_context("child")
    assert (
        child is not None
        and child["status"] == "running"
        and child["child_turn_id"] == "turn-2"
    )
    _subagent_turn(service, child="child", turn="turn-2", status="completed")
    with db.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM coworker_events WHERE message_id LIKE 'internal:%'"
        ).fetchone()[0]
    assert count == 2


def test_automatic_retention_keeps_origin_for_child_and_queued_wake(
    tmp_path: Path,
) -> None:
    service, db, _backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    server = FakeAppServer(service)
    service.app_server = cast(Any, server)

    def spawn(prompt: str, root: str) -> None:
        _subagent_activity(service, root=root, child="child", kind="started")
        _subagent_turn(service, child="child", turn="turn-1", status=None)
        return None

    server.run_hook = spawn
    service.receive(_event("m1", sender="alice", text="需要保留", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        conn.execute(
            "UPDATE messages SET inserted_at = '2020-01-01T00:00:00+00:00', sent_at = '2020-01-01T00:00:00+00:00'"
        )
    retention = RetentionService(store=db, config=service.config, base_dir=tmp_path)
    retention.prune(protect_pending=True)
    assert db.get_message("m1")["text"] == "需要保留"  # type: ignore[index]
    _subagent_turn(service, child="child", turn="turn-1", status="completed")
    retention.prune(protect_pending=True)
    assert db.get_message("m1")["text"] == "需要保留"  # type: ignore[index]


def test_direct_mode_shares_main_group_session_and_does_not_enqueue_a_second_reply(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(
        tmp_path, send_mode="agent_direct", chat_layout="group"
    )
    for message_id, sender in [("m1", "alice"), ("m2", "bob")]:
        event = _event(message_id, sender=sender, text="请总结", mention=True)
        message = cast(dict[str, Any], cast(dict[str, Any], event["event"])["message"])
        for field in ("thread_id", "root_id", "parent_id"):
            message.pop(field, None)
        service.receive(event, "bot")
        assert service.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert feishu.chat_history_calls == 0
    assert "机器人身份自动回复" in backend.prompts[-1]
    assert "本轮发送方式：模型直发" in backend.prompts[-1]
    assert "请求来自群主消息区时，默认回复原消息" in backend.prompts[-1]
    assert "符合以下 JSON Schema" not in backend.prompts[-1]
    assert "meegle" in backend.prompts[-1] and "lark-doc" in backend.prompts[-1]
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 0
        )
    snapshot = snapshot_from_local_store(
        db.path, app_id="app", bot_open_id="bot", limit=20
    )
    assert snapshot.chat_layout == "group"
    replay = replay_recorded_events(snapshot)
    assert [turn["resume"] for turn in replay["turns"]] == [False, True]


def test_send_mode_switch_reuses_session_after_restart_and_changes_instructions(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(tmp_path, send_mode="project")
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    service.config.coworker.send_mode = "agent_direct"
    db.import_product_policy_from_config(service.config, replace=True)
    restarted = FakeCoworkerService(
        store=db,
        config=service.config,
        feishu=feishu,
        logger=JSONLLogger(tmp_path / "restart.jsonl"),
        base_dir=tmp_path,
        app_id="app",
        dry_run=True,
        backend_factory=lambda *_args: backend,
    )
    restarted.receive(_event("m2", sender="bob", text="继续", mention=True), "bot")
    assert restarted.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert "回复或委派由项目发送" in backend.prompts[0]
    assert "本轮发送方式：项目代发" in backend.prompts[0]
    assert "机器人身份自动回复" in backend.prompts[1]
    assert "请求来自话题时，默认回复到原话题" in backend.prompts[1]
    assert "使用 `lark-cli` 并指定 `--as bot` 回复当前消息" in backend.prompts[1]
    assert "不要请求项目转发" not in backend.prompts[1]


def test_direct_partner_mention_resumes_session_without_project_delegation(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(
        tmp_path, send_mode="agent_direct", partner_bots={"Box": "box"}
    )
    service.receive(_event("m1", sender="alice", text="请委派", mention=True), "bot")
    assert service.process_next()
    service.receive(
        _event("m2", sender="box", sender_type="bot", text="任务已完成", mention=True),
        "bot",
    )
    assert service.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert "任务已完成" in backend.prompts[-1]
    with db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM coworker_delegations").fetchone()[0] == 0
        )


def test_direct_mode_does_not_fetch_unmentioned_partner_message(tmp_path: Path) -> None:
    service, db, backend, feishu = _service(
        tmp_path, send_mode="agent_direct", partner_bots={"Box": "box"}
    )

    def unavailable(**_kwargs: Any) -> MessagePage:
        raise AssertionError("unmentioned partner message does not need a snapshot")

    feishu.get_messages = unavailable  # type: ignore[method-assign]
    service.receive(
        _event(
            "m1",
            sender="box",
            sender_type="bot",
            text="other discussion",
            mention=False,
        ),
        "bot",
    )
    assert service.process_next() and backend.prompts == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "completed"
        )


def test_switch_from_direct_to_project_reuses_session_and_queues_only_project_reply(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(tmp_path, send_mode="agent_direct")
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    service.config.coworker.send_mode = "project"
    db.import_product_policy_from_config(service.config, replace=True)
    restarted = FakeCoworkerService(
        store=db,
        config=service.config,
        feishu=feishu,
        logger=JSONLLogger(tmp_path / "restart-project.jsonl"),
        base_dir=tmp_path,
        app_id="app",
        dry_run=True,
        backend_factory=lambda *_args: backend,
    )
    restarted.receive(_event("m2", sender="bob", text="继续", mention=True), "bot")
    assert restarted.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert "回复或委派由项目发送" in backend.prompts[-1]
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 1
        )


def test_changing_send_mode_without_restart_does_not_switch_execution(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path, send_mode="project")
    service.config.coworker.send_mode = "agent_direct"
    db.import_product_policy_from_config(service.config, replace=True)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert not service.process_next()
    assert backend.prompts == []
    with db.connect() as conn:
        row = conn.execute("SELECT status, error FROM coworker_events").fetchone()
    assert row is not None and row[0] == "queued" and row[1] is None


def test_disabling_coworker_keeps_unprocessed_events_queued(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    service.config.coworker.enabled = False
    db.import_product_policy_from_config(service.config, replace=True)
    assert not service.process_next()
    assert backend.prompts == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0] == "queued"
        )


def test_source_failure_retries_before_agent_without_reordering_topic(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(tmp_path, send_mode="agent_direct")
    original = feishu.get_messages

    def unavailable(**_kwargs: Any) -> MessagePage:
        raise RuntimeError("temporary failure")

    feishu.get_messages = unavailable  # type: ignore[method-assign]
    service.receive(_event("m1", sender="alice", text="first", mention=True), "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="bob", text="second", mention=True), "bot")
    assert not service.process_next()
    assert backend.prompts == []
    feishu.get_messages = original  # type: ignore[method-assign]
    db.clock = lambda: "2099-01-01T00:00:00+00:00"
    assert service.process_next() and service.process_next()
    assert "first" in backend.prompts[0] and "second" in backend.prompts[1]


def test_stopping_worker_waits_until_current_agent_finishes(tmp_path: Path) -> None:
    service, _db, backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def block_agent() -> None:
        entered.set()
        assert release.wait(timeout=5)

    backend.on_call = block_agent
    service.receive(_event("m1", sender="alice", text="work", mention=True), "bot")
    worker = CoworkerWorker(
        service, runtime_allows_processing=lambda: True, wake_daemon=lambda: None
    )
    worker.start()
    assert entered.wait(timeout=3)

    def stop_worker() -> None:
        worker.stop()
        stopped.set()

    stopper = threading.Thread(target=stop_worker)
    stopper.start()
    try:
        assert not stopped.wait(timeout=0.2)
    finally:
        release.set()
        stopper.join(timeout=5)
    assert stopped.is_set()


def test_legacy_imported_policy_keeps_project_sending(tmp_path: Path) -> None:
    service, db, _backend, feishu = _service(tmp_path, send_mode="project")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT policy_json FROM product_policies WHERE key = 'ai_coworker'"
        ).fetchone()
        assert row is not None
        policy = json.loads(row[0])
        policy.pop("send_mode")
        conn.execute(
            "UPDATE product_policies SET policy_json = ? WHERE key = 'ai_coworker'",
            (json.dumps(policy),),
        )
    restarted = FakeCoworkerService(
        store=db,
        config=service.config,
        feishu=feishu,
        logger=JSONLLogger(tmp_path / "restart.jsonl"),
        base_dir=tmp_path,
        app_id="app",
        dry_run=True,
    )
    assert restarted.current_policy() is not None
    assert restarted.current_policy().send_mode == "project"  # type: ignore[union-attr]


@pytest.mark.parametrize("history_source", ["live_on_demand", "local_cache"])
def test_unmentioned_main_group_message_is_available_after_later_mention(
    tmp_path: Path, history_source: Literal["live_on_demand", "local_cache"]
) -> None:
    service, db, backend, feishu = _service(
        tmp_path,
        send_mode="agent_direct",
        chat_layout="group",
        history_source=history_source,
    )
    feishu.chat_history_items = [
        {
            "message_id": "m1",
            "chat_id": "group",
            "sender_id": "alice",
            "text": "未 @ 的讨论",
        }
    ]
    for message_id, text, mention in [
        ("m1", "未 @ 的讨论", False),
        ("m2", "请总结", True),
    ]:
        event = _event(message_id, sender="alice", text=text, mention=mention)
        message = cast(dict[str, Any], cast(dict[str, Any], event["event"])["message"])
        for field in ("thread_id", "root_id", "parent_id"):
            message.pop(field, None)
        service.receive(event, "bot")
        assert service.process_next()
    assert len(backend.prompts) == 1
    assert "未 @ 的讨论" not in backend.prompts[0]
    assert f"历史来源：`{history_source}`" in backend.prompts[0]
    if history_source == "local_cache":
        result = read_cached_history(db.path, app_id="app", chat_id="group")
        assert any(item["text"] == "未 @ 的讨论" for item in result["messages"])
    else:
        assert feishu.chat_history_calls == 0
        page = feishu.list_chat_messages(
            chat_id="group",
            start=None,
            end=None,
            page_size=30,
            as_identity="bot",
            order="desc",
        )
        assert page.items[0]["text"] == "未 @ 的讨论"


def test_mention_mode_shares_topic_session_and_recovers_unmentioned_history(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="bob", text="改下周", mention=False), "bot")
    assert service.process_next()
    service.receive(_event("m3", sender="bob", text="有什么变化", mention=True), "bot")
    assert service.process_next()

    assert backend.session_ids == [None, "codex-session"]
    assert "截止周五" not in backend.prompts[0]
    assert "历史来源：`live_on_demand`" in backend.prompts[0]
    assert all(identity == "bot" for identity in feishu.identities)
    with db.connect() as conn:
        actions = conn.execute(
            "SELECT execution_mode, payload_json FROM actions WHERE kind = 'send_reply' ORDER BY id"
        ).fetchall()
    assert len(actions) == 2
    assert all(row["execution_mode"] == "dry_run" for row in actions)
    assert all(json.loads(row["payload_json"])["identity"] == "bot" for row in actions)


def test_cache_does_not_invoke_model_until_mentioned(tmp_path: Path) -> None:
    service, db, backend, feishu = _service(tmp_path, history_source="local_cache")
    service.receive(_event("m1", sender="alice", text="截止周五", mention=False), "bot")
    assert service.process_next()
    assert backend.prompts == []
    service.receive(_event("m2", sender="bob", text="总结", mention=True), "bot")
    assert service.process_next()
    assert "截止周五" not in backend.prompts[0]
    result = read_cached_history(db.path, app_id="app", chat_id="group")
    assert any(item["text"] == "截止周五" for item in result["messages"])
    assert feishu.identities == ["bot", "bot"]


@pytest.mark.parametrize("created", ["1794900000000", 1794900000000])
def test_cache_backfill_accepts_feishu_millisecond_timestamps(
    tmp_path: Path, created: str | int
) -> None:
    service, db, _backend, feishu = _service(tmp_path, history_source="local_cache")
    feishu.list_thread_messages = lambda **_kwargs: MessagePage(  # type: ignore[method-assign]
        items=[
            {
                "message_id": "old",
                "chat_id": "group",
                "thread_id": "thread",
                "text": "Earlier discussion",
                "create_time": created,
            }
        ]
    )
    service.receive(
        _event("current", sender="alice", text="summarize", mention=True), "bot"
    )
    assert service.process_next()
    result = read_cached_history(db.path, app_id="app", chat_id="group")
    old = next(item for item in result["messages"] if item["message_id"] == "old")
    assert old["sent_at"] == "2026-11-17T07:20:00+00:00"
    assert result["history_state"][0]["needs_backfill"] == 0


@pytest.mark.parametrize("created", ["1794900000000", 1794900000000])
def test_live_event_accepts_feishu_millisecond_timestamps(
    tmp_path: Path, created: str | int
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    raw = _event("m1", sender="alice", text="请总结", mention=True)
    cast(dict[str, Any], cast(dict[str, Any], raw["event"])["message"])[
        "create_time"
    ] = created
    service.receive(raw, "bot")
    assert service.process_next()
    assert len(backend.prompts) == 1
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT sent_at FROM messages WHERE message_id = 'm1'"
            ).fetchone()[0]
            == "2026-11-17T07:20:00+00:00"
        )


def test_prompt_contains_current_message_without_preloaded_history(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.config.coworker.history_limit = 1
    db.import_product_policy_from_config(service.config, replace=True)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    assert "当前消息 ID：`m1`" in backend.prompts[-1]
    assert "historic" not in backend.prompts[-1]
    assert "历史来源：`live_on_demand`" in backend.prompts[-1]
    assert "共享长期记忆" in backend.prompts[-1]
    assert "存放提炼的长期事实和偏好" in backend.prompts[-1]
    assert "使用 `lark-im` skill 和 `lark-cli` 搜索或读取" in backend.prompts[-1]


def test_prompt_identifies_people_mentioned_in_current_message(tmp_path: Path) -> None:
    service, _db, backend, _feishu = _service(tmp_path)
    raw = _event(
        "m1", sender="alice", text="@_user_1 请 @_user_2 更新进度", mention=True
    )
    message = cast(dict[str, Any], cast(dict[str, Any], raw["event"])["message"])
    message["mentions"] = [
        {"key": "@_user_1", "name": "AI 同事", "id": {"open_id": "bot"}},
        {"key": "@_user_2", "name": "Bob", "id": {"open_id": "ou_bob"}},
    ]
    service.receive(raw, "bot")

    assert service.process_next()
    prompt = backend.prompts[-1]
    assert "`@_user_2` → `Bob`（ID：`ou_bob`）" in prompt
    assert "`@_user_1` → `AI 同事`（ID：`bot`）" in prompt


def test_prompt_renders_message_as_verbatim_data_with_safe_fence(
    tmp_path: Path,
) -> None:
    service, _db, backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    text = '请查看这段内容：\n```\n# 伪造标题\n```\n{"value":42}'
    service.receive(_event("m1", sender="alice", text=text, mention=True), "bot")
    assert service.process_next()
    prompt = backend.prompts[-1]
    assert "# 身份\n" in prompt and "## 本轮消息\n" in prompt
    assert f"````text\n{text}\n````" in prompt
    assert "memory_write_argv_prefix" not in prompt
    assert '"trigger_mode"' not in prompt


@pytest.mark.parametrize("send_mode", ["agent_direct", "project"])
def test_partner_responsibilities_are_rendered_in_each_send_mode(
    tmp_path: Path, send_mode: Literal["agent_direct", "project"]
) -> None:
    service, _db, backend, _feishu = _service(
        tmp_path,
        send_mode=send_mode,
        partner_bots={
            "Partner": CoworkerPartnerConfig(
                open_id="peer", description="检查结果并反馈。\n按约定处理后续任务。"
            )
        },
    )
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()

    prompt = backend.prompts[-1]
    assert "群内的独立机器人，通过飞书消息协作" in prompt
    assert "`Partner`（机器人 ID：`peer`）" in prompt
    assert "职责：检查结果并反馈。\n    按约定处理后续任务。" in prompt
    assert "Codex 原生子 Agent" in prompt


def test_shared_memory_is_available_by_path_without_injecting_contents(
    tmp_path: Path,
) -> None:
    service, _db, backend, _feishu = _service(tmp_path, send_mode="agent_direct")
    path = tmp_path / "data/coworker-shared-memory.md.dry-run"
    SharedMemory(path).append([MemoryEntry("unique preference", "message=old")])
    service.receive(_event("m1", sender="alice", text="hello", mention=True), "bot")
    assert service.process_next()
    assert str(path) in backend.prompts[0]
    assert "unique preference" not in backend.prompts[0]


def test_edited_message_without_bot_mention_does_not_start_agent(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(tmp_path, send_mode="agent_direct")
    feishu.get_messages = lambda **_kwargs: MessagePage(  # type: ignore[method-assign]
        items=[
            {
                "message_id": "m1",
                "chat_id": "group",
                "msg_type": "text",
                "content": {"text": "Edited without the bot"},
                "mentions": [],
            }
        ]
    )
    service.receive(_event("m1", sender="alice", text="original", mention=True), "bot")
    assert service.process_next() and backend.prompts == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "completed"
        )


def test_cached_attachment_reference_is_included_in_later_prompt(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path, history_source="local_cache")
    event = _event("image", sender="alice", text="", mention=False)
    message = cast(dict[str, Any], cast(dict[str, Any], event["event"])["message"])
    message["message_type"] = "image"
    message["content"] = json.dumps({"image_key": "img_example"})
    service.receive(event, "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="bob", text="看看图片", mention=True), "bot")
    assert service.process_next()
    assert "img_example" not in backend.prompts[-1]
    result = read_cached_history(db.path, app_id="app", chat_id="group")
    assert any(
        {"type": "image", "file_key": "img_example"} in item["resources"]
        for item in result["messages"]
    )


@pytest.mark.parametrize("history_source", ["live_on_demand", "local_cache"])
def test_followups_trigger_only_after_topic_is_watched(
    tmp_path: Path, history_source: Literal["live_on_demand", "local_cache"]
) -> None:
    service, _db, backend, _feishu = _service(
        tmp_path, history_source=history_source, trigger_mode="followups"
    )
    service.receive(_event("m1", sender="alice", text="先讨论", mention=False), "bot")
    assert service.process_next()
    assert backend.prompts == []
    service.receive(_event("m2", sender="alice", text="参与一下", mention=True), "bot")
    assert service.process_next()
    service.receive(_event("m3", sender="bob", text="时间变了", mention=False), "bot")
    assert service.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert "时间变了" in backend.prompts[-1]


def test_cache_backfills_again_after_reconnect_gap(tmp_path: Path) -> None:
    service, _db, _backend, feishu = _service(tmp_path, history_source="local_cache")
    service.receive(_event("m1", sender="alice", text="第一次", mention=True), "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="alice", text="第二次", mention=True), "bot")
    assert service.process_next()
    assert len(feishu.identities) == 3  # two snapshots, one history backfill
    service.events.mark_history_gaps("app")
    service.receive(_event("m3", sender="alice", text="第三次", mention=True), "bot")
    assert service.process_next()
    assert len(feishu.identities) == 5


def test_owner_and_bot_identity_are_not_treated_as_shadow_takeover(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="owner", text="请总结", mention=True), "bot")
    assert service.process_next()
    assert len(backend.prompts) == 1
    service.receive(_event("self", sender="bot", text="self", mention=True), "bot")
    assert not service.process_next()
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM routing_audits WHERE route = 'human_taken_over'"
            ).fetchone()[0]
            == 0
        )


def test_partner_delegation_matches_only_the_sent_request(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(
        tmp_path,
        partner_bots={
            "Box": CoworkerPartnerConfig(open_id="peer", description="检查构建")
        },
    )
    backend.outputs = [{"action": "delegate", "text": "检查构建", "partner": "Box"}]
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        delegation = conn.execute(
            "SELECT id, request_action_id, status FROM coworker_delegations"
        ).fetchone()
        action = conn.execute(
            "SELECT payload_json FROM actions WHERE id = ?",
            (delegation["request_action_id"],),
        ).fetchone()
        assert (
            '<at user_id="peer">Box</at>' in json.loads(action["payload_json"])["text"]
        )
        conn.execute(
            "UPDATE actions SET status = 'sent', result_json = ? WHERE id = ?",
            (
                json.dumps({"sent_message_id": "outgoing"}),
                delegation["request_action_id"],
            ),
        )
    service.receive(
        _event(
            "unrelated",
            sender="peer",
            sender_type="bot",
            parent_id="root",
            text="别的事",
            mention=False,
        ),
        "bot",
    )
    assert service.process_next()
    assert len(backend.prompts) == 1
    service.receive(
        _event(
            "result",
            sender="peer",
            sender_type="bot",
            parent_id="outgoing",
            text="构建成功",
            mention=False,
        ),
        "bot",
    )
    assert service.process_next()
    assert backend.session_ids == [None, "codex-session"]
    assert "构建成功" in backend.prompts[-1]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, result_message_id FROM coworker_delegations"
        ).fetchone()
    assert tuple(row) == ("completed", "result")
    # A safe retry after a rejected steer must still recognize the same reply;
    # a different message cannot reuse a completed delegation.
    assert service.events.match_delegation(
        app_id="app",
        chat_id="group",
        topic_key="thread",
        peer_id="peer",
        message_id="result",
        parent_id="outgoing",
        text="构建成功",
    ) == ("matched", 1)
    assert (
        service.events.match_delegation(
            app_id="app",
            chat_id="group",
            topic_key="thread",
            peer_id="peer",
            message_id="different",
            parent_id="outgoing",
            text="构建成功",
        )
        is None
    )


def test_partner_correlation_uses_current_source_content(tmp_path: Path) -> None:
    service, db, backend, feishu = _service(tmp_path, partner_bots={"Box": "peer"})
    backend.outputs = [{"action": "delegate", "text": "检查构建", "partner": "Box"}]
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        delegation = conn.execute(
            "SELECT id, request_action_id FROM coworker_delegations"
        ).fetchone()
        conn.execute(
            "UPDATE actions SET status = 'sent', result_json = ? WHERE id = ?",
            (
                json.dumps({"sent_message_id": "outgoing"}),
                delegation["request_action_id"],
            ),
        )
    service.receive(
        _event(
            "result",
            sender="peer",
            sender_type="bot",
            parent_id="root",
            text=f"完成 {delegation['id']}",
            mention=False,
        ),
        "bot",
    )
    feishu.sources["result"] = "编辑后已移除协作编号"
    assert service.process_next()
    assert len(backend.prompts) == 1
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_delegations").fetchone()[0]
            == "pending"
        )


def test_partner_timeout_is_queued_once_after_the_request_was_sent(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path, partner_bots={"Box": "peer"})
    backend.outputs = [{"action": "delegate", "text": "检查构建", "partner": "Box"}]
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()
    assert db.expire_coworker_delegations() == 0
    with db.connect() as conn:
        conn.execute(
            "UPDATE actions SET status = 'sent', updated_at = '2020-01-01T00:00:00+00:00' WHERE kind = 'send_reply'"
        )
    assert db.expire_coworker_delegations() == 1
    assert db.expire_coworker_delegations() == 0
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_delegations").fetchone()[0]
            == "timed_out"
        )
        actions = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'send_reply' AND status = 'pending'"
        ).fetchall()
    assert len(actions) == 1
    assert json.loads(actions[0]["payload_json"])["source"] == "coworker_timeout"


def test_cancelled_partner_dispatch_releases_pending_delegation(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path, partner_bots={"Box": "peer"})
    backend.outputs = [{"action": "delegate", "text": "检查构建", "partner": "Box"}]
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        action_id = conn.execute(
            "SELECT request_action_id FROM coworker_delegations"
        ).fetchone()[0]
    db.cancel_dispatch_action(action_id)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_delegations").fetchone()[0]
            == "failed"
        )
    backend.outputs = [{"action": "delegate", "text": "重新检查", "partner": "Box"}]
    service.receive(_event("m2", sender="alice", text="再检查", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM coworker_delegations WHERE status = 'pending'"
            ).fetchone()[0]
            == 1
        )


def test_explicit_remember_is_persisted_before_reply(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    backend.outputs = [{"action": "reply", "text": "记住了。", "partner": None}]
    memory = tmp_path / "data/coworker-shared-memory.md.dry-run"

    def write_memory() -> None:
        SharedMemory(memory).append([MemoryEntry("演示截止周五", "message=m1")])

    backend.on_call = write_memory
    service.receive(
        _event("m1", sender="alice", text="请记住演示截止周五", mention=True), "bot"
    )
    assert service.process_next()
    assert "演示截止周五" in memory.read_text(encoding="utf-8")
    assert "message=m1" in memory.read_text(encoding="utf-8")
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("text", ["请记下发布在周一", "你记住了吗？"])
def test_project_mode_does_not_override_agent_reply_using_memory_keywords(
    tmp_path: Path, text: str
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    backend.outputs = [{"action": "reply", "text": "按上下文回答", "partner": None}]
    service.receive(_event("m1", sender="alice", text=text, mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM actions WHERE kind='send_reply'"
            ).fetchone()[0]
        )
    assert "按上下文回答" in payload["text"]
    assert "我还不能确定" not in payload["text"]


def test_backend_failure_during_memory_tool_call_holds_turn_for_review(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    path = tmp_path / "data/coworker-shared-memory.md.dry-run"
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "elsewhere")
    backend.outputs = [{"action": "reply", "text": "记住了。", "partner": None}]

    def write_memory() -> None:
        SharedMemory(path).append([MemoryEntry("事实", "message=m1")])

    backend.on_call = write_memory
    service.receive(
        _event("m1", sender="alice", text="记住这个事实", mention=True), "bot"
    )
    assert service.process_next()
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "needs_review"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 0
        )


def test_background_maintenance_extracts_new_fact_without_explicit_remember(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(
        _event("m1", sender="alice", text="这个项目每周五演示", mention=True), "bot"
    )
    assert service.process_next()
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET received_at = '2020-01-01T00:00:00+00:00'"
        )
    path = tmp_path / "data/coworker-shared-memory.md.dry-run"

    def write_memory() -> None:
        SharedMemory(path).append([MemoryEntry("这个项目每周五演示", "messages=m1")])

    backend.on_call = write_memory
    assert service.maintain_memory_if_due()
    memory = (tmp_path / "data/coworker-shared-memory.md.dry-run").read_text(
        encoding="utf-8"
    )
    assert "这个项目每周五演示" in memory and "messages=m1" in memory
    assert backend.session_ids == [None, None]
    assert not service.maintain_memory_if_due()


def test_background_memory_agent_can_choose_how_to_search_and_write(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="讨论演示", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET received_at = '2020-01-01T00:00:00+00:00'"
        )
    assert service.maintain_memory_if_due()
    assert not (tmp_path / "data/coworker-shared-memory.md.dry-run").exists()
    assert db.get_checkpoint("coworker.memory_maintenance")["state"] == "idle"  # type: ignore[index]
    assert "跨话题搜索" in backend.prompts[-1]
    assert "符合以下 JSON Schema" not in backend.prompts[-1]
    assert "已有记忆" not in backend.prompts[-1]
    assert not service.maintain_memory_if_due()


def test_daily_memory_runs_without_events_and_catches_up_once(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    db.update_coworker_policy({"memory_maintenance_time": "03:00"}, actor="test")
    now = [datetime(2026, 11, 18, 2, 59).astimezone().isoformat()]
    db.clock = lambda: now[0]
    assert not service.maintain_memory_if_due()
    now[0] = datetime(2026, 11, 18, 3, 0).astimezone().isoformat()
    assert service.maintain_memory_if_due()
    assert not service.maintain_memory_if_due()
    assert "上次成功运行：无记录" in backend.prompts[-1]
    now[0] = datetime(2026, 11, 21, 10, 0).astimezone().isoformat()
    assert service.maintain_memory_if_due()
    assert not service.maintain_memory_if_due()
    assert len(backend.prompts) == 2
    checkpoint = db.get_checkpoint("coworker.memory_maintenance")
    assert checkpoint and checkpoint["last_scheduled_date"] == "2026-11-21"
    assert checkpoint["memory_file_changed"] is False
    assert "上次成功运行" in backend.prompts[-1]
    now[0] = datetime(2026, 11, 22, 3, 0).astimezone().isoformat()
    assert service.maintain_memory_if_due()


def test_daily_memory_failure_retries_at_next_scheduled_time(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    now = [datetime(2026, 11, 18, 1).astimezone().isoformat()]
    db.clock = lambda: now[0]
    backend.natural_task_session = lambda *args, **kwargs: AgentRunResult(
        argv=[], exit_code=1, error="failed"
    )  # type: ignore[method-assign]
    assert service.maintain_memory_if_due()
    assert not service.maintain_memory_if_due()
    assert db.get_checkpoint("coworker.memory_maintenance")["state"] == "failed"  # type: ignore[index]
    now[0] = datetime(2026, 11, 19, 1).astimezone().isoformat()
    assert service.maintain_memory_if_due()


def test_daily_memory_timeout_waits_for_review_instead_of_retrying(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    now = [datetime(2026, 11, 18, 1).astimezone().isoformat()]
    db.clock = lambda: now[0]
    calls: list[str] = []

    def timed_out(*_args: Any, **_kwargs: Any) -> AgentRunResult:
        calls.append("run")
        return AgentRunResult(argv=[], exit_code=None, error="timeout", timed_out=True)

    backend.natural_task_session = timed_out  # type: ignore[method-assign]
    assert service.maintain_memory_if_due()
    assert db.get_checkpoint("coworker.memory_maintenance")["state"] == "needs_review"  # type: ignore[index]
    now[0] = datetime(2026, 11, 19, 1).astimezone().isoformat()
    assert not service.maintain_memory_if_due()
    assert calls == ["run"]


def test_memory_backend_construction_failure_remains_retryable(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path)
    db.clock = lambda: datetime(2026, 11, 18, 1).astimezone().isoformat()

    def fail_factory(*_args: Any) -> Any:
        raise ValueError("invalid backend configuration")

    service.backend_factory = fail_factory
    assert service.maintain_memory_if_due()
    assert db.get_checkpoint("coworker.memory_maintenance")["state"] == "failed"  # type: ignore[index]


def test_dispatch_rechecks_source_edits_and_coworker_policy(tmp_path: Path) -> None:
    service, db, _backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        action_id = conn.execute(
            "SELECT id FROM actions WHERE kind = 'send_reply'"
        ).fetchone()[0]
    action = db.get_action(action_id)
    assert action is not None
    dispatcher = Dispatcher(
        store=db,
        feishu_client=feishu,  # type: ignore[arg-type]
        config=service.config,
        logger=service.logger,
    )
    feishu.edited = True
    result = dispatcher._call_send(action, dry_run=True)
    assert not result.ok
    assert result.error == "coworker_source_changed_or_deleted"
    assert not feishu.sent
    feishu.edited = False
    service.config.coworker.enabled = False
    db.import_product_policy_from_config(service.config, replace=True)
    result = dispatcher._call_send(action, dry_run=True)
    assert not result.ok
    assert result.error == "coworker_policy_disabled_or_changed"
    assert not feishu.sent


def test_model_uses_current_source_content_when_event_was_edited(
    tmp_path: Path,
) -> None:
    service, _db, backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="旧内容", mention=True), "bot")
    feishu.sources["m1"] = "已编辑的新内容"
    assert service.process_next()
    assert "已编辑的新内容" in backend.prompts[-1]
    assert "旧内容" not in backend.prompts[-1]


def test_current_message_can_be_json_text(tmp_path: Path) -> None:
    service, _db, backend, _feishu = _service(tmp_path)
    service.receive(
        _event("m1", sender="alice", text='{"value":42}', mention=True), "bot"
    )
    assert service.process_next()
    assert '{"value":42}' in backend.prompts[-1]


def test_edited_at_all_message_does_not_trigger_the_model(tmp_path: Path) -> None:
    service, _db, backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="旧 @", mention=True), "bot")
    feishu.sources["m1"] = "@all 请注意"
    assert service.process_next()
    assert backend.prompts == []


def test_changed_source_content_blocks_send_without_update_time(tmp_path: Path) -> None:
    service, db, _backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="原内容", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        action_id = conn.execute(
            "SELECT id FROM actions WHERE kind = 'send_reply'"
        ).fetchone()[0]
    action = db.get_action(action_id)
    assert action is not None
    feishu.sources["m1"] = "更新内容"
    dispatcher = Dispatcher(
        store=db,
        feishu_client=feishu,  # type: ignore[arg-type]
        config=service.config,
        logger=service.logger,
    )
    result = dispatcher._call_send(action, dry_run=True)
    assert not result.ok
    assert result.error == "coworker_source_changed_or_deleted"


def test_missing_source_content_is_held_before_agent_runs(tmp_path: Path) -> None:
    service, db, backend, feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    feishu.sources["m1"] = None
    assert service.process_next()
    assert backend.prompts == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "needs_review"
        )


def test_coworker_reply_can_be_approved_with_bot_identity(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path)
    db.upsert_chat_product_policy(
        {
            "chat_id": "group",
            "auto_reply": False,
            "bot_joined": True,
            "reply_identity": "bot",
            "allow_user_fallback": False,
        },
        actor="test",
    )
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT short_id FROM approvals WHERE status = 'pending'"
        ).fetchone()
    assert row is not None
    result = OperatorCommandService(db).approve(
        row["short_id"], actor="owner", execution_mode="dry_run"
    )
    assert result.status == "applied"
    with db.connect() as conn:
        action = conn.execute(
            "SELECT payload_json FROM actions WHERE kind = 'send_reply'"
        ).fetchone()
    assert json.loads(action["payload_json"])["identity"] == "bot"


def test_model_can_request_owner_approval_even_when_auto_reply_enabled(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    backend.outputs = [
        {
            "action": "reply",
            "text": "需要确认后回复",
            "partner": None,
            "approval_required": True,
            "approval_reason": "需确认承诺",
        }
    ]
    service.receive(
        _event("m1", sender="alice", text="请确认交付", mention=True), "bot"
    )
    assert service.process_next()
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind = 'send_reply'"
            ).fetchone()[0]
            == 0
        )


def test_coworker_approval_can_only_be_applied_in_coworker_scope(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    backend.outputs = [
        {
            "action": "reply",
            "text": "需确认",
            "partner": None,
            "approval_required": True,
        }
    ]
    service.receive(_event("m1", sender="alice", text="请确认", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        approval = conn.execute(
            "SELECT short_id FROM approvals WHERE status='pending'"
        ).fetchone()[0]
    ignored = OperatorCommandService(db, approval_scope="shadow").approve(
        approval, actor="owner", command_id="test-command", execution_mode="dry_run"
    )
    assert not ignored.changed
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM approvals WHERE short_id=?", (approval,)
            ).fetchone()[0]
            == "pending"
        )
        assert conn.execute("SELECT COUNT(*) FROM approval_commands").fetchone()[0] == 0
    accepted = OperatorCommandService(db, approval_scope="coworker").approve(
        approval, actor="owner", command_id="test-command", execution_mode="dry_run"
    )
    assert accepted.changed


def test_coworker_task_is_not_selected_for_shadow_watch(tmp_path: Path) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    backend.outputs = [
        {
            "action": "reply",
            "text": "需确认",
            "partner": None,
            "approval_required": True,
        }
    ]
    service.receive(_event("m1", sender="alice", text="请确认", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        task_id = conn.execute("SELECT task_id FROM approvals").fetchone()[0]
        conn.execute(
            "UPDATE tasks SET status='watching', watch_until='2099-01-01T00:00:00Z' WHERE id=?",
            (task_id,),
        )
    assert db.task_is_coworker(task_id)
    assert db.get_active_tasks_for_chat("group", now="2026-11-18T00:00:00Z") == []
    assert db.list_active_watch_targets(now="2026-11-18T00:00:00Z") == []


def test_partner_delegation_approval_preserves_correlation_and_bot_identity(
    tmp_path: Path,
) -> None:
    service, db, backend, feishu = _service(
        tmp_path,
        partner_bots={
            "Box": CoworkerPartnerConfig(open_id="peer", description="检查构建")
        },
    )
    db.upsert_chat_product_policy(
        {
            "chat_id": "group",
            "auto_reply": False,
            "bot_joined": True,
            "reply_identity": "bot",
            "allow_user_fallback": False,
        },
        actor="test",
    )
    backend.outputs = [{"action": "delegate", "text": "检查构建", "partner": "Box"}]
    service.receive(_event("m1", sender="alice", text="检查", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        pending = conn.execute(
            "SELECT id, request_action_id, request_approval_id FROM coworker_delegations"
        ).fetchone()
        approval = conn.execute(
            "SELECT short_id FROM approvals WHERE id = ?",
            (pending["request_approval_id"],),
        ).fetchone()
    assert pending["request_action_id"] is None
    result = OperatorCommandService(db).approve(
        approval["short_id"], actor="owner", execution_mode="dry_run"
    )
    assert result.status == "applied"
    with db.connect() as conn:
        delegation = conn.execute(
            "SELECT id, request_action_id FROM coworker_delegations"
        ).fetchone()
    action = db.get_action(delegation["request_action_id"])
    assert action is not None
    assert action.payload["source"] == "coworker_delegate_approval"
    assert delegation["id"] in action.payload["text"]
    dispatcher = Dispatcher(
        store=db,
        feishu_client=feishu,  # type: ignore[arg-type]
        config=service.config,
        logger=service.logger,
    )
    assert dispatcher._call_send(action, dry_run=True).ok


def test_expired_queued_event_content_is_scrubbed_and_held(tmp_path: Path) -> None:
    service, db, _backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="待处理", mention=True), "bot")
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET received_at = '2020-01-01T00:00:00+00:00'"
        )
    retention = RetentionService(store=db, config=service.config, base_dir=tmp_path)
    assert retention.prune(dry_run=True).content_candidates["coworker_events"] == 1
    assert retention.prune().content_scrubbed["coworker_events"] == 1
    with db.connect() as conn:
        event = conn.execute(
            "SELECT status, raw_json, error FROM coworker_events"
        ).fetchone()
    assert tuple(event) == ("needs_review", None, "content_expired")


def test_queued_event_does_not_run_after_group_policy_reactivation(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="总结", mention=True), "bot")
    db.delete_chat_product_policy("group", actor="test")
    db.upsert_chat_product_policy(
        {
            "chat_id": "group",
            "auto_reply": True,
            "bot_joined": True,
            "reply_identity": "bot",
        },
        actor="test",
    )
    assert service.process_next()
    assert backend.prompts == []
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM coworker_events").fetchone()
    assert row["status"] == "needs_review"


def test_remember_does_not_write_after_group_policy_reactivation(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="记住这个", mention=True), "bot")
    backend.outputs = [
        {"action": "reply", "text": "记住了", "partner": None, "remember": "一个事实"}
    ]

    def reactivate() -> None:
        db.delete_chat_product_policy("group", actor="test")
        db.upsert_chat_product_policy(
            {"chat_id": "group", "auto_reply": True, "bot_joined": True}, actor="test"
        )

    backend.on_call = reactivate
    assert service.process_next()
    assert not (tmp_path / "data/coworker-shared-memory.md.dry-run").exists()
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM coworker_events").fetchone()[0]
            == "needs_review"
        )
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_memory_maintenance_stops_after_group_policy_reactivation(
    tmp_path: Path,
) -> None:
    service, db, backend, _feishu = _service(tmp_path)
    service.receive(_event("m1", sender="alice", text="一个事实", mention=True), "bot")
    assert service.process_next()
    with db.connect() as conn:
        conn.execute(
            "UPDATE coworker_events SET received_at = '2020-01-01T00:00:00+00:00'"
        )
    backend.outputs = [{"facts": [{"text": "一个事实", "source_message_ids": ["m1"]}]}]

    def reactivate() -> None:
        db.delete_chat_product_policy("group", actor="test")
        db.upsert_chat_product_policy(
            {"chat_id": "group", "auto_reply": True, "bot_joined": True}, actor="test"
        )

    backend.on_call = reactivate
    assert service.maintain_memory_if_due()
    assert not (tmp_path / "data/coworker-shared-memory.md.dry-run").exists()
    assert db.get_checkpoint("coworker.memory_maintenance")["state"] == "needs_review"  # type: ignore[index]


@pytest.mark.parametrize("history_source", ["live_on_demand", "local_cache"])
@pytest.mark.parametrize("trigger_mode", ["mention", "followups"])
def test_recorded_event_replay_is_offline_and_preserves_context_choices(
    history_source: Literal["live_on_demand", "local_cache"],
    trigger_mode: Literal["mention", "followups"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay attempted network access")

    monkeypatch.setattr("socket.socket", no_network)
    snapshot = CoworkerReplaySnapshot(
        app_id="app",
        chat_id="group",
        bot_open_id="bot",
        history_source=history_source,
        trigger_mode=trigger_mode,
        events=[
            _event("m1", sender="alice", text="讨论周五", mention=False),
            _event("m2", sender="bob", text="总结", mention=True),
            _event("m3", sender="alice", text="改为周一", mention=False),
            _event("m4", sender="bob", text="再总结", mention=True),
        ],
    )
    result = replay_recorded_events(snapshot)
    turns = result["turns"]
    assert result["offline"] is True and result["backend"] == "silent_fake"
    assert len(turns) == (2 if trigger_mode == "mention" else 3)
    assert turns[0]["resume"] is False and turns[-1]["resume"] is True
    assert turns[0]["message_ids"] == ["m2"]
    assert turns[-1]["message_ids"] == ["m4"]
    assert all(turn["history_source"] == history_source for turn in turns)


def test_recorded_replay_refuses_another_group() -> None:
    snapshot = CoworkerReplaySnapshot(
        app_id="app",
        chat_id="another-group",
        bot_open_id="bot",
        events=[_event("m1", sender="alice", text="hello", mention=True)],
    )
    with pytest.raises(ValueError, match="selected app and group"):
        replay_recorded_events(snapshot)


def test_export_reconstructs_only_stored_group_messages_for_offline_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from feishu_shadow_agent.cli import main

    service, db, _backend, _feishu = _service(tmp_path, history_source="local_cache")
    first = _event("m1", sender="alice", text="讨论周五", mention=False)
    cast(dict[str, Any], first["header"])["token"] = "sensitive-token"
    service.receive(first, "bot")
    assert service.process_next()
    service.receive(_event("m2", sender="bob", text="总结", mention=True), "bot")
    assert service.process_next()

    snapshot = snapshot_from_local_store(
        db.path, app_id="app", bot_open_id="bot", limit=20
    )
    assert snapshot.source_kind == "reconstructed_local"
    assert "sensitive-token" not in snapshot.model_dump_json()
    report = replay_recorded_events(snapshot)
    assert len(report["turns"]) == 1
    assert report["turns"][0]["message_ids"] == ["m2"]
    path = tmp_path / "recorded.json"
    path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "coworker",
                "replay",
                "--snapshot",
                str(path),
                "--trigger-mode",
                "followups",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"offline": true' in output
    assert "讨论周五" not in output and "sensitive-token" not in output

    service.config.storage.sqlite_path = str(db.path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(service.config.model_dump(mode="json")), encoding="utf-8"
    )
    export_path = tmp_path / "private-snapshot.json"
    command = [
        "coworker",
        "export-replay",
        "--config",
        str(config_path),
        "--app-id",
        "app",
        "--bot-open-id",
        "bot",
        "--output",
        str(export_path),
    ]
    assert main(command) == 0
    assert stat.S_IMODE(export_path.stat().st_mode) == 0o600
    assert main(command) == 2
