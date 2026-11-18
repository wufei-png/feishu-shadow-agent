from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from .agent_backend import AgentRunResult
from .agent_output_schema import agent_output_schema
from .agent_skill_context import (
    append_codex_skill_mentions,
    append_explicit_context_paths,
)
from .config import CodexConfig


class AppServerError(RuntimeError):
    """The app-server connection or request failed."""


class AppServerRejected(AppServerError):
    """The server explicitly rejected a request without applying it."""


class TurnNotActive(AppServerError):
    """A turn ended before a follow-up could be steered into it."""


@dataclass
class _Request:
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    failure: str | None = None


@dataclass
class _Turn:
    done: threading.Event = field(default_factory=threading.Event)
    turn_id: str | None = None
    status: str | None = None
    message: str = ""
    error: str | None = None


class CodexAppServer:
    """One persistent Codex process shared by independently routed topics."""

    def __init__(
        self,
        *,
        config: CodexConfig,
        skill_names: list[str],
        explicit_context_paths: list[str],
        on_notification: Callable[[dict[str, Any]], None],
        request_timeout: float = 30,
    ):
        self.config = config
        self.skill_names = skill_names
        self.explicit_context_paths = explicit_context_paths
        self.on_notification = on_notification
        self.request_timeout = request_timeout
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._requests: dict[int, _Request] = {}
        self._turns: dict[str, _Turn] = {}
        self._next_id = 0
        self._failed: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._process is not None:
                raise AppServerError("Codex app-server has already been started")
            self._process = subprocess.Popen(  # noqa: S603
                [self.config.path or "codex", "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            self._reader = threading.Thread(
                target=self._read, name="codex-app-server-reader", daemon=True
            )
            self._reader.start()
        try:
            self._request(
                "initialize",
                {"clientInfo": {"name": "feishu_shadow_agent", "version": "0.1.0"}},
            )
            self._write({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        self._fail("Codex app-server stopped")

    def run_turn(
        self,
        prompt: str,
        *,
        session_id: str | None,
        cwd: str | Path | None,
        output_model: type[BaseModel] | None,
        on_thread: Callable[[str], None],
    ) -> AgentRunResult:
        started = time.monotonic()
        thread_id = session_id
        turn: _Turn | None = None
        try:
            if thread_id is None:
                params: dict[str, Any] = {}
                if cwd is not None:
                    params["cwd"] = str(cwd)
                if self.config.model:
                    params["model"] = self.config.model
                response = self._request("thread/start", params)
                thread_id = _thread_id(response)
                prompt = append_explicit_context_paths(
                    prompt, self.explicit_context_paths
                )
                prompt = append_codex_skill_mentions(prompt, self.skill_names)
            else:
                self._request("thread/resume", {"threadId": thread_id})
            on_thread(thread_id)
            turn = _Turn()
            with self._lock:
                if thread_id in self._turns:
                    raise AppServerError("a turn is already active for this session")
                self._turns[thread_id] = turn
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            }
            if cwd is not None:
                params["cwd"] = str(cwd)
            if self.config.model:
                params["model"] = self.config.model
            if self.config.reasoning_effort:
                params["effort"] = self.config.reasoning_effort
            if output_model is not None:
                params["outputSchema"] = agent_output_schema(output_model)
            response = self._request("turn/start", params)
            with self._lock:
                if turn.turn_id is None:
                    turn.turn_id = _turn_id(response)
            if not turn.done.wait(timeout=self.config.timeout_seconds):
                self._interrupt_best_effort(thread_id, turn.turn_id)
                return AgentRunResult(
                    argv=[],
                    exit_code=None,
                    session_id=thread_id,
                    error="Codex app-server turn timed out",
                    timed_out=True,
                    latency_ms=_elapsed_ms(started),
                    backend_provider="codex",
                )
            if turn.status != "completed":
                return AgentRunResult(
                    argv=[],
                    exit_code=None,
                    session_id=thread_id,
                    error=turn.error or f"Codex turn {turn.status or 'failed'}",
                    latency_ms=_elapsed_ms(started),
                    backend_provider="codex",
                )
            output: Any | None = None
            if output_model is not None:
                try:
                    output = json.loads(turn.message)
                except json.JSONDecodeError:
                    return AgentRunResult(
                        argv=[],
                        exit_code=None,
                        session_id=thread_id,
                        error="Codex did not return valid structured output",
                        latency_ms=_elapsed_ms(started),
                        backend_provider="codex",
                    )
            return AgentRunResult(
                argv=[],
                exit_code=0,
                stdout=turn.message,
                json_data=output,
                session_id=thread_id,
                latency_ms=_elapsed_ms(started),
                backend_provider="codex",
            )
        except (AppServerError, OSError, ValueError) as exc:
            return AgentRunResult(
                argv=[],
                exit_code=None,
                session_id=thread_id,
                error=str(exc),
                latency_ms=_elapsed_ms(started),
                backend_provider="codex",
            )
        finally:
            if thread_id is not None:
                with self._lock:
                    if turn is not None and self._turns.get(thread_id) is turn:
                        del self._turns[thread_id]

    def steer(self, thread_id: str, prompt: str) -> None:
        with self._lock:
            turn = self._turns.get(thread_id)
            turn_id = None if turn is None else turn.turn_id
        if not turn_id or turn is None or turn.done.is_set():
            raise TurnNotActive("no active Codex turn")
        try:
            self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
        except AppServerRejected as exc:
            raise TurnNotActive("Codex did not accept the follow-up") from exc

    def active_turn_id(self, thread_id: str) -> str | None:
        with self._lock:
            turn = self._turns.get(thread_id)
            return None if turn is None or turn.done.is_set() else turn.turn_id

    def is_healthy(self) -> bool:
        with self._lock:
            return (
                self._failed is None
                and self._process is not None
                and self._process.poll() is None
            )

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def _interrupt_best_effort(self, thread_id: str, turn_id: str | None) -> None:
        if turn_id:
            with contextlib.suppress(AppServerError):
                self.interrupt(thread_id, turn_id)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._failed:
                raise AppServerError(self._failed)
            self._next_id += 1
            request_id = self._next_id
            pending = _Request()
            self._requests[request_id] = pending
        try:
            self._write({"id": request_id, "method": method, "params": params})
            if not pending.done.wait(timeout=self.request_timeout):
                raise AppServerError(f"Codex app-server request timed out: {method}")
            if pending.failure is not None:
                raise AppServerError(pending.failure)
            response = pending.response or {}
            if isinstance(response.get("error"), dict):
                error = cast(dict[str, Any], response["error"])
                raise AppServerRejected(
                    f"{method} rejected: {error.get('message', 'unknown')}"
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"Codex app-server returned no result: {method}")
            return cast(dict[str, Any], result)
        finally:
            with self._lock:
                self._requests.pop(request_id, None)

    def _write(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                raise AppServerError("Codex app-server is not running")
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AppServerError("Codex app-server connection closed") from exc

    def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    value: Any = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    self._handle(cast(dict[str, Any], value))
        finally:
            self._fail("Codex app-server connection closed")

    def _handle(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(request_id, int) and not method:
            with self._lock:
                pending = self._requests.get(request_id)
                if pending is not None:
                    pending.response = message
                    pending.done.set()
            return
        if request_id is not None and isinstance(method, str):
            self._reject_server_request(request_id, method)
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        payload = cast(dict[str, Any], params)
        thread_id = payload.get("threadId")
        completed_turn: _Turn | None = None
        if isinstance(thread_id, str):
            with self._lock:
                turn = self._turns.get(thread_id)
                if turn is not None:
                    if method == "turn/started":
                        turn.turn_id = _turn_id(payload)
                    elif method == "item/completed":
                        item = _mapping(payload.get("item"))
                        if (
                            turn.turn_id is None
                            or payload.get("turnId") == turn.turn_id
                        ) and item.get("type") == "agentMessage":
                            text = item.get("text")
                            if isinstance(text, str):
                                turn.message = text
                    elif method == "turn/completed":
                        completed = _mapping(payload.get("turn"))
                        if completed and (
                            turn.turn_id is None or completed.get("id") == turn.turn_id
                        ):
                            turn.status = str(completed.get("status") or "failed")
                            error = _mapping(completed.get("error"))
                            if error:
                                turn.error = str(error.get("message") or "turn failed")
                            completed_turn = turn
        try:
            self.on_notification(message)
        except Exception:  # noqa: BLE001
            self._fail("Codex app-server notification could not be recorded")
        else:
            if completed_turn is not None:
                completed_turn.done.set()

    def _reject_server_request(self, request_id: Any, method: str) -> None:
        try:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                self._write({"id": request_id, "result": {"decision": "decline"}})
            else:
                self._write(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "interactive request unavailable",
                        },
                    }
                )
        except AppServerError:
            pass

    def _fail(self, error: str) -> None:
        with self._lock:
            self._failed = error
            for pending in self._requests.values():
                pending.failure = error
                pending.done.set()
            for turn in self._turns.values():
                turn.status = "failed"
                turn.error = error
                turn.done.set()


def _thread_id(response: dict[str, Any]) -> str:
    thread = _mapping(response.get("thread"))
    value = thread.get("id")
    if not isinstance(value, str) or not value:
        raise AppServerError("Codex app-server did not return a thread ID")
    return value


def _turn_id(response: dict[str, Any]) -> str:
    turn = _mapping(response.get("turn"))
    value = turn.get("id")
    if not isinstance(value, str) or not value:
        raise AppServerError("Codex app-server did not return a turn ID")
    return value


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _mapping(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}
