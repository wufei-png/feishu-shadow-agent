from __future__ import annotations

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
        """#!/usr/bin/env python3
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    msg = json.loads(line)
    method, ident, params = msg.get('method'), msg.get('id'), msg.get('params', {})
    if method == 'initialize':
        emit({'id': ident, 'result': {}})
    elif method == 'thread/start':
        emit({'id': ident, 'result': {'thread': {'id': 'thread-1'}}})
    elif method == 'thread/resume':
        emit({'id': ident, 'result': {'thread': {'id': params['threadId']}}})
    elif method == 'turn/start':
        thread = params['threadId']
        emit({'id': ident, 'result': {'turn': {'id': 'turn-1'}}})
        emit({'method': 'turn/started', 'params': {'threadId': thread, 'turn': {'id': 'turn-1'}}})
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
