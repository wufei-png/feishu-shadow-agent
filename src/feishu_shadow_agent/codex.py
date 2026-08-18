from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from .agent_backend import AgentRunResult
from .agent_output_schema import agent_output_schema
from .agent_skill_context import (
    append_codex_skill_mentions,
    append_explicit_context_paths,
)
from .config import (
    AutoContextMode,
    CodexConfig,
    ConfigScopeMode,
    ReplyPostprocessConfig,
    ToolPermissionsProfile,
)
from .prompt import (
    FollowupTaskSessionOutput,
    InitialTaskSessionOutput,
    OwnerStyleRefreshOutput,
    ReplyPostprocessOutput,
    TaskRouterOutput,
)
from .prompt_instructions import prompt_text

# Test and embedding callers may provide a runner with a narrower timeout
# contract; the subprocess adapter itself accepts the provider's optional value.
CodexRunner = Callable[..., AgentRunResult]

TASK_SESSION_DEVELOPER_INSTRUCTIONS = prompt_text(
    """
    This is a non-interactive structured Task Session.
    Use tools silently and do not emit progress or commentary messages.
    Return exactly one final JSON object.
    """
)


@dataclass(frozen=True)
class CodexExecutionPolicy:
    root_args: list[str]
    exec_args: list[str]


def codex_execution_policy(profile: ToolPermissionsProfile) -> CodexExecutionPolicy:
    if profile == "read_only":
        return CodexExecutionPolicy(
            root_args=["--search", "--ask-for-approval", "never"],
            exec_args=["--sandbox", "read-only"],
        )
    if profile == "full_access":
        return CodexExecutionPolicy(
            root_args=["--search"],
            exec_args=["--dangerously-bypass-approvals-and-sandbox"],
        )
    raise ValueError(f"unknown tool permissions profile: {profile}")


class CodexCliClient:
    provider = "codex"

    def __init__(
        self,
        *,
        config: CodexConfig,
        tool_permissions: ToolPermissionsProfile = "read_only",
        config_scope: ConfigScopeMode = "isolated",
        auto_context: AutoContextMode = "disabled",
        reply_postprocess: ReplyPostprocessConfig | None = None,
        session_skill_names: Sequence[str] | None = None,
        explicit_context_paths: Sequence[str | Path] = (),
        cwd: str | Path | None = None,
        runner: CodexRunner | None = None,
    ):
        self.config = config
        self.execution_policy = codex_execution_policy(tool_permissions)
        self.read_only_execution_policy = codex_execution_policy("read_only")
        self.config_scope = config_scope
        self.auto_context = auto_context
        self.reply_postprocess_config = reply_postprocess or ReplyPostprocessConfig()
        configured_skill_names = (
            config.skills if session_skill_names is None else session_skill_names
        )
        self.session_skill_names = list(dict.fromkeys(configured_skill_names))
        self.explicit_context_paths = [str(path) for path in explicit_context_paths]
        self.path = config.path or "codex"
        self.cwd = None if cwd is None else Path(cwd)
        self._runner = runner

    def build_exec_command(
        self,
        *,
        output_schema_path: str | Path,
        output_path: str | Path,
        session_id: str | None = None,
        cwd: str | Path | None = None,
        execution_policy: CodexExecutionPolicy | None = None,
        model: str | None = None,
        developer_instructions: str | None = None,
    ) -> list[str]:
        policy = execution_policy or self.execution_policy
        argv = [self.path, *policy.root_args]
        if self.config.reasoning_effort:
            argv.extend(
                [
                    "-c",
                    "model_reasoning_effort="
                    f"{json.dumps(self.config.reasoning_effort)}",
                ]
            )
        if developer_instructions:
            argv.extend(
                [
                    "-c",
                    f"developer_instructions={json.dumps(developer_instructions)}",
                ]
            )
        argv.extend(
            [
                "exec",
                "--skip-git-repo-check",
                *policy.exec_args,
            ]
        )
        if self.config_scope == "isolated":
            argv.append("--ignore-user-config")
        if self.auto_context == "disabled":
            argv.append("--ignore-rules")
        run_cwd = self.cwd if cwd is None else Path(cwd)
        if run_cwd is not None:
            argv.extend(["--cd", str(run_cwd)])
        argv.extend(
            [
                "--json",
                "--output-schema",
                str(output_schema_path),
                "--output-last-message",
                str(output_path),
            ]
        )
        effective_model = self.config.model if model is None else model
        if effective_model:
            argv.extend(["--model", effective_model])
        if session_id:
            argv.extend(["resume", session_id, "-"])
        else:
            argv.append("-")
        return argv

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._run(prompt, output_model=TaskRouterOutput, cwd=cwd)

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        output_model: type[BaseModel] = (
            InitialTaskSessionOutput
            if session_id is None
            else FollowupTaskSessionOutput
        )
        return self._run(
            prompt,
            output_model=output_model,
            session_id=session_id,
            cwd=cwd,
            include_session_skills=session_id is None,
            developer_instructions=TASK_SESSION_DEVELOPER_INSTRUCTIONS,
        )

    def structured_output(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        return self._run(
            prompt, output_model=output_model, session_id=session_id, cwd=cwd
        )

    def reply_postprocess(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._run(
            prompt,
            output_model=ReplyPostprocessOutput,
            cwd=cwd,
            execution_policy=self.read_only_execution_policy,
            model=self.reply_postprocess_config.model,
        )

    def owner_style_refresh(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._run(
            prompt,
            output_model=OwnerStyleRefreshOutput,
            cwd=cwd,
            execution_policy=self.read_only_execution_policy,
            model=self.reply_postprocess_config.model,
        )

    def _run(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id: str | None = None,
        cwd: str | Path | None = None,
        execution_policy: CodexExecutionPolicy | None = None,
        model: str | None = None,
        include_session_skills: bool = False,
        developer_instructions: str | None = None,
    ) -> AgentRunResult:
        effective_prompt = prompt
        run_cwd = self.cwd if cwd is None else Path(cwd)
        if include_session_skills:
            effective_prompt = append_explicit_context_paths(
                effective_prompt, self.explicit_context_paths
            )
            effective_prompt = append_codex_skill_mentions(
                effective_prompt, self.session_skill_names
            )
        schema_path = _write_temp_json(agent_output_schema(output_model))
        output_path = _empty_temp_path()
        argv = self.build_exec_command(
            output_schema_path=schema_path,
            output_path=output_path,
            session_id=session_id,
            cwd=run_cwd,
            execution_policy=execution_policy,
            model=model,
            developer_instructions=developer_instructions,
        )
        try:
            if self._runner is not None:
                result = self._runner(
                    argv, self.config.timeout_seconds, effective_prompt, run_cwd
                )
            else:
                result = _run_subprocess(
                    argv,
                    self.config.timeout_seconds,
                    stdin=effective_prompt,
                    cwd=run_cwd,
                )
            parsed_session_id = result.session_id or _parse_thread_id(result.stdout)
            if not result.ok:
                return _with_codex_provider(result, session_id=parsed_session_id)
            json_text = _last_message_text(result.stdout, output_path)
            if not json_text:
                return AgentRunResult(
                    argv=result.argv,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    session_id=parsed_session_id,
                    error="Codex did not produce a final message",
                    latency_ms=result.latency_ms,
                    backend_provider=self.provider,
                )
            try:
                json_data: Any = json.loads(json_text)
            except json.JSONDecodeError as exc:
                return AgentRunResult(
                    argv=result.argv,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    session_id=parsed_session_id,
                    error=f"final message was not valid JSON: {exc}",
                    latency_ms=result.latency_ms,
                    backend_provider=self.provider,
                )
            return AgentRunResult(
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                json_data=json_data,
                session_id=parsed_session_id,
                latency_ms=result.latency_ms,
                backend_provider=self.provider,
            )
        finally:
            _unlink_quietly(schema_path)
            _unlink_quietly(output_path)

    def requested_skill_names(self) -> list[str]:
        return list(self.session_skill_names)


def _run_subprocess(
    argv: Sequence[str],
    timeout_seconds: int | None,
    *,
    stdin: str | None = None,
    cwd: Path | None = None,
) -> AgentRunResult:
    started = time.monotonic()
    try:
        # argv is assembled by the configured Codex adapter and shell=False
        # prevents shell interpretation of model/config values.
        completed = subprocess.run(  # noqa: S603
            list(argv),
            input=stdin,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return AgentRunResult(
            argv=list(argv),
            exit_code=None,
            stdout=_subprocess_text(exc.stdout),
            stderr=_subprocess_text(exc.stderr),
            error=f"command timed out after {timeout_seconds}s",
            timed_out=True,
            latency_ms=_latency_ms(started),
            backend_provider="codex",
        )
    except OSError as exc:
        return AgentRunResult(
            argv=list(argv),
            exit_code=None,
            error=str(exc),
            latency_ms=_latency_ms(started),
            backend_provider="codex",
        )
    return AgentRunResult(
        argv=list(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        session_id=_parse_thread_id(completed.stdout),
        error=None
        if completed.returncode == 0
        else (completed.stderr.strip() or completed.stdout.strip()),
        latency_ms=_latency_ms(started),
        backend_provider="codex",
    )


def _last_message_text(stdout: str, output_path: Path) -> str:
    try:
        text = output_path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    event_text = _parse_last_agent_message(stdout)
    if event_text:
        return event_text.strip()
    return stdout.strip()


def _parse_thread_id(stdout: str) -> str | None:
    for event in _jsonl_events(stdout):
        value = event.get("thread_id")
        if isinstance(value, str) and value:
            return value
    return None


def _parse_last_agent_message(stdout: str) -> str | None:
    text: str | None = None
    for event in _jsonl_events(stdout):
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_map = cast(dict[str, Any], item)
        if item_map.get("type") == "agent_message" and isinstance(
            item_map.get("text"), str
        ):
            text = item_map["text"]
    return text


def _jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, Any], event))
    return events


def _with_codex_provider(
    result: AgentRunResult, *, session_id: str | None
) -> AgentRunResult:
    return AgentRunResult(
        argv=result.argv,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        json_data=result.json_data,
        session_id=session_id,
        error=result.error,
        timed_out=result.timed_out,
        latency_ms=result.latency_ms,
        backend_provider="codex",
    )


def _write_temp_json(data: dict[str, Any]) -> Path:
    fd, name = tempfile.mkstemp(
        prefix="feishu-shadow-agent-codex-schema-", suffix=".json"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return Path(name)


def _empty_temp_path() -> Path:
    fd, name = tempfile.mkstemp(
        prefix="feishu-shadow-agent-codex-output-", suffix=".json"
    )
    os.close(fd)
    return Path(name)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _latency_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _subprocess_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
