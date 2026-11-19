from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from feishu_shadow_agent.codex_app_server import AppServerError, CodexAppServer
from feishu_shadow_agent.config import CodexConfig


def _server_executable(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + f"REQUEST_LOG = {str(tmp_path / 'requests.jsonl')!r}\n"
        + """
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    msg = json.loads(line)
    method, ident, params = msg.get('method'), msg.get('id'), msg.get('params', {})
    if method in ('thread/start', 'thread/resume'):
        with open(REQUEST_LOG, 'a', encoding='utf-8') as requests:
            requests.write(json.dumps({'method': method, 'params': params}) + '\\n')
    if method == 'initialize':
        emit({'id': ident, 'result': {}})
    elif method == 'config/read':
        emit({'id': ident, 'result': {'config': {'mcp_servers': {'example': {}}}}})
    elif method == 'mcpServerStatus/list':
        emit({'id': ident, 'result': {'data': [{'name': 'example', 'tools': {}}], 'nextCursor': None}})
    elif method == 'thread/start':
        emit({'id': ident, 'result': {'thread': {'id': 'thread-1'}}})
    elif method == 'thread/resume':
        assert params.get('excludeTurns') is True
        emit({'id': ident, 'result': {'thread': {'id': params['threadId']}}})
    elif method == 'turn/start':
        if params['input'][0]['text'] == 'dry-run-safety':
            assert params.get('approvalPolicy') == 'never'
            assert params.get('sandboxPolicy') == {'type': 'readOnly', 'networkAccess': False}
        if params['input'][0]['text'] == 'production-native-tools':
            assert 'approvalPolicy' not in params and 'sandboxPolicy' not in params
        thread = params['threadId']
        emit({'id': ident, 'result': {'turn': {'id': 'turn-1'}}})
        emit({'method': 'turn/started', 'params': {'threadId': thread, 'turn': {'id': 'turn-1'}}})
        if params['input'][0]['text'] == 'topic-tool':
            emit({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'turnId': 'turn-1', 'tool': 'read_feishu_topic',
                'arguments': {'thread': 'topic-id', 'page_token': None}}})
            continue
        if params['input'][0]['text'] == 'group-tool':
            emit({'id': 'tool-request', 'method': 'item/tool/call', 'params': {
                'threadId': thread, 'turnId': 'turn-1', 'tool': 'read_feishu_group_history',
                'arguments': {'page_token': None}}})
            continue
        if 'outputSchema' in params:
            emit({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': 'turn-1', 'item': {'type': 'agentMessage', 'text': '{"answer":"ok"}'}}})
            emit({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': 'turn-1', 'status': 'completed'}}})
    elif method == 'turn/steer':
        if params['input'][0]['text'] == 'disconnect':
            sys.exit(0)
        if params['input'][0]['text'] == 'reject':
            emit({'id': ident, 'error': {'message': 'turn ended'}})
            continue
        emit({'id': ident, 'result': {'turnId': 'turn-1'}})
        emit({'method': 'item/completed', 'params': {'threadId': params['threadId'], 'turnId': 'turn-1', 'item': {'type': 'subAgentActivity', 'kind': 'completed', 'agentThreadId': 'child-1'}}})
        emit({'method': 'item/completed', 'params': {'threadId': params['threadId'], 'turnId': 'turn-1', 'item': {'type': 'agentMessage', 'text': 'updated'}}})
        emit({'method': 'turn/completed', 'params': {'threadId': params['threadId'], 'turn': {'id': 'turn-1', 'status': 'completed'}}})
    elif method == 'turn/interrupt':
        emit({'id': ident, 'result': {}})
    elif method == 'thread/unsubscribe':
        emit({'id': ident, 'result': {'status': 'unsubscribed'}})
    elif method == 'thread/loaded/list':
        emit({'id': ident, 'result': {'data': [], 'nextCursor': None}})
    elif method is None and ident == 'tool-request':
        result = msg['result']
        assert result['success'] and 'visible' in result['contentItems'][0]['text']
        emit({'method': 'item/completed', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'agentMessage', 'text': '{"answer":"ok"}'}}})
        emit({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}})
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


class _Output(BaseModel):
    answer: str


def test_app_server_resumes_structured_turn_without_replacing_native_settings(
    tmp_path: Path,
) -> None:
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _event: None,
    )
    server.start()
    try:
        thread_ids: list[str] = []
        result = server.run_turn(
            "synthetic",
            session_id="existing-session",
            cwd=tmp_path,
            output_model=_Output,
            on_thread=thread_ids.append,
        )
    finally:
        server.close()
    assert result.ok and result.json_data == {"answer": "ok"}
    assert result.session_id == "existing-session" == thread_ids[0]


def test_private_memory_settings_apply_on_start_and_resume(tmp_path: Path) -> None:
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _event: None,
    )
    server.start()
    try:
        first = server.run_turn(
            "one",
            session_id=None,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
            private=True,
        )
        second = server.run_turn(
            "two",
            session_id=first.session_id,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
            private=True,
        )
    finally:
        server.close()
    assert first.ok and second.ok
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    assert [item["method"] for item in requests] == ["thread/start", "thread/resume"]
    assert all(
        item["params"]["config"]
        == {"memories.use_memories": False, "memories.generate_memories": False}
        for item in requests
    )
    assert requests[1]["params"]["excludeTurns"] is True


def test_release_checks_loaded_threads_and_reload_restores_private_settings(
    tmp_path: Path,
) -> None:
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _event: None,
    )
    server.start()
    try:
        server.release_thread("thread-1", timeout_seconds=1)
        server.reload_thread("thread-1", private=True, cwd=str(tmp_path))
    finally:
        server.close()
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    config = requests[-1]["params"]["config"]
    assert config["thread_unload_delay_secs"] == 1
    assert config["memories.use_memories"] is False
    assert config["memories.generate_memories"] is False


def test_dry_run_disables_native_tools_on_start_and_resume(tmp_path: Path) -> None:
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _event: None,
        on_read_topic=lambda _thread, _topic, _token, _turn, _active: {},
    )
    server.start()
    try:
        first = server.run_turn(
            "dry-run-safety",
            session_id=None,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
            dry_run_safety=True,
        )
        second = server.run_turn(
            "dry-run-safety",
            session_id=first.session_id,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
            dry_run_safety=True,
        )
    finally:
        server.close()
    assert first.ok and second.ok
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    for request in requests:
        assert request["params"]["sandbox"] == "read-only"
        assert request["params"]["approvalPolicy"] == "never"
        config = request["params"]["config"]
        assert config["mcp_servers.example.enabled"] is False
        assert config["features.shell_tool"] is False
        assert config["features.unified_exec"] is False
        assert config["features.apps"] is False
        assert "dynamicTools" not in request["params"]


def test_production_inherits_native_tools_on_start_resume_and_reload(
    tmp_path: Path,
) -> None:
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _event: None,
        on_read_topic=lambda _thread, _topic, _token, _turn, _active: {},
    )
    server.start()
    try:
        for session_id in (None, "thread-1"):
            result = server.run_turn(
                "production-native-tools",
                session_id=session_id,
                cwd=tmp_path,
                output_model=_Output,
                on_thread=lambda _: None,
            )
            assert result.ok
        server.reload_thread("thread-1", private=True, cwd=str(tmp_path))
    finally:
        server.close()
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    for request in requests:
        params = request["params"]
        assert "sandbox" not in params and "approvalPolicy" not in params
        config = params.get("config", {})
        assert not any(key.startswith(("mcp_servers.", "features.")) for key in config)
        assert "web_search" not in config
    assert requests[0]["params"]["dynamicTools"][0]["name"] == "read_feishu_topic"


def test_production_topic_tool_is_dispatched_to_controlled_reader(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def read_topic(
        thread_id: str, topic: str, token: str | None, _turn: str, _active: bool
    ) -> dict[str, Any]:
        calls.append((thread_id, topic, token))
        return {"text": "visible"}

    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _: None,
        on_read_topic=read_topic,
    )
    server.start()
    try:
        result = server.run_turn(
            "topic-tool",
            session_id=None,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
        )
    finally:
        server.close()
    assert result.ok and calls == [("thread-1", "topic-id", None)]


def test_production_group_history_tool_dispatches_active_turn(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None, str, bool]] = []

    def read_group(
        thread_id: str, token: str | None, turn_id: str, active: bool
    ) -> dict[str, Any]:
        calls.append((thread_id, token, turn_id, active))
        return {"text": "visible"}

    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda _: None,
        on_read_group=read_group,
    )
    server.start()
    try:
        result = server.run_turn(
            "group-tool",
            session_id=None,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
        )
    finally:
        server.close()
    assert result.ok and calls == [("thread-1", None, "turn-1", True)]


def test_notification_failure_cannot_be_reported_as_a_successful_turn(
    tmp_path: Path,
) -> None:
    def notify(message: dict[str, Any]) -> None:
        if message.get("method") == "turn/completed":
            raise RuntimeError("persistence failed")

    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=notify,
    )
    server.start()
    try:
        result = server.run_turn(
            "synthetic",
            session_id=None,
            cwd=tmp_path,
            output_model=_Output,
            on_thread=lambda _: None,
        )
    finally:
        server.close()
    assert not result.ok
    assert "notification could not be recorded" in (result.error or "")


def test_app_server_steers_running_turn_and_delivers_child_events(
    tmp_path: Path,
) -> None:
    notifications: list[dict[str, Any]] = []
    active = threading.Event()

    def notify(message: dict[str, Any]) -> None:
        notifications.append(message)
        if message.get("method") == "turn/started":
            active.set()

    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=notify,
    )
    results: list[Any] = []
    server.start()
    try:
        worker = threading.Thread(
            target=lambda: results.append(
                server.run_turn(
                    "wait",
                    session_id=None,
                    cwd=tmp_path,
                    output_model=None,
                    on_thread=lambda _thread: None,
                )
            )
        )
        worker.start()
        assert active.wait(timeout=3)
        server.steer("thread-1", "updated")
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert results[0].ok and results[0].stdout == "updated"
        assert any(
            (event.get("params") or {}).get("item", {}).get("kind") == "completed"
            for event in notifications
        )
    finally:
        server.close()


def test_app_server_connection_failure_does_not_report_a_safe_rejection(
    tmp_path: Path,
) -> None:
    active = threading.Event()
    server = CodexAppServer(
        config=CodexConfig(path=str(_server_executable(tmp_path)), timeout_seconds=5),
        skill_names=[],
        explicit_context_paths=[],
        on_notification=lambda event: (
            active.set() if event.get("method") == "turn/started" else None
        ),
    )
    server.start()
    worker = threading.Thread(
        target=lambda: server.run_turn(
            "wait",
            session_id=None,
            cwd=tmp_path,
            output_model=None,
            on_thread=lambda _thread: None,
        )
    )
    worker.start()
    try:
        assert active.wait(timeout=3)
        with pytest.raises(AppServerError, match="connection closed"):
            server.steer("thread-1", "disconnect")
    finally:
        worker.join(timeout=3)
        server.close()
