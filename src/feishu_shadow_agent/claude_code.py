from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .agent_backend import AgentRunResult
from .agent_output_schema import agent_output_schema
from .config import (
    AutoContextMode,
    ClaudeCodeConfig,
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

ClaudeCodeRunner = Callable[
    [list[str], int | None, str | None, Path | None], AgentRunResult
]

READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch")
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


@dataclass(frozen=True)
class ClaudeCodeExecutionPolicy:
    args: list[str]


def claude_code_execution_policy(
    profile: ToolPermissionsProfile,
) -> ClaudeCodeExecutionPolicy:
    if profile == "read_only":
        tools = ",".join(READ_ONLY_TOOLS)
        return ClaudeCodeExecutionPolicy(
            args=[
                "--permission-mode",
                "dontAsk",
                "--tools",
                tools,
                "--allowedTools",
                tools,
            ]
        )
    if profile == "full_access":
        return ClaudeCodeExecutionPolicy(
            args=[
                "--permission-mode",
                "bypassPermissions",
                "--dangerously-skip-permissions",
                "--tools",
                "default",
            ]
        )
    raise ValueError(f"unknown tool permissions profile: {profile}")


class ClaudeCodeCliClient:
    provider = "claude_code"

    def __init__(
        self,
        *,
        config: ClaudeCodeConfig,
        tool_permissions: ToolPermissionsProfile = "read_only",
        config_scope: ConfigScopeMode = "isolated",
        auto_context: AutoContextMode = "disabled",
        reply_postprocess: ReplyPostprocessConfig | None = None,
        cwd: str | Path | None = None,
        runner: ClaudeCodeRunner | None = None,
    ):
        self.config = config
        self.execution_policy = claude_code_execution_policy(tool_permissions)
        self.read_only_execution_policy = claude_code_execution_policy("read_only")
        self.config_scope = config_scope
        self.auto_context = auto_context
        self.reply_postprocess_config = reply_postprocess or ReplyPostprocessConfig()
        self.path = config.path or "claude"
        self.cwd = None if cwd is None else Path(cwd)
        self._runner = runner

    def build_print_command(
        self,
        *,
        output_schema: dict[str, Any],
        session_id: str | None = None,
        cwd: str | Path | None = None,
        execution_policy: ClaudeCodeExecutionPolicy | None = None,
        model: str | None = None,
    ) -> list[str]:
        policy = execution_policy or self.execution_policy
        argv = [
            self.path,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(output_schema, ensure_ascii=False, separators=(",", ":")),
            *policy.args,
            "--mcp-config",
            EMPTY_MCP_CONFIG,
            "--strict-mcp-config",
        ]
        if self.config_scope == "isolated":
            argv.extend(["--setting-sources", "local"])
        if self.auto_context == "disabled":
            argv.append("--safe-mode")
        run_cwd = self.cwd if cwd is None else Path(cwd)
        if run_cwd is not None:
            argv.extend(["--add-dir", str(run_cwd)])
        effective_model = self.config.model if model is None else model
        if effective_model:
            argv.extend(["--model", effective_model])
        if session_id:
            argv.extend(["--resume", session_id])
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
            prompt, output_model=output_model, session_id=session_id, cwd=cwd
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
        execution_policy: ClaudeCodeExecutionPolicy | None = None,
        model: str | None = None,
    ) -> AgentRunResult:
        run_cwd = self.cwd if cwd is None else Path(cwd)
        argv = self.build_print_command(
            output_schema=agent_output_schema(output_model),
            session_id=session_id,
            cwd=run_cwd,
            execution_policy=execution_policy,
            model=model,
        )
        if self._runner is not None:
            result = self._runner(argv, self.config.timeout_seconds, prompt, run_cwd)
        else:
            result = _run_subprocess(
                argv, self.config.timeout_seconds, stdin=prompt, cwd=run_cwd
            )
        parsed_session_id = result.session_id or _parse_session_id(result.stdout)
        if not result.ok:
            return _with_claude_code_provider(result, session_id=parsed_session_id)
        parsed = _parse_structured_output(result.stdout)
        if parsed.error is not None:
            return AgentRunResult(
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                session_id=parsed.session_id or parsed_session_id,
                error=parsed.error,
                latency_ms=result.latency_ms,
                backend_provider=self.provider,
            )
        return AgentRunResult(
            argv=result.argv,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            json_data=parsed.json_data,
            session_id=parsed.session_id or parsed_session_id,
            latency_ms=result.latency_ms,
            backend_provider=self.provider,
        )


def _run_subprocess(
    argv: Sequence[str],
    timeout_seconds: int | None,
    *,
    stdin: str | None = None,
    cwd: Path | None = None,
) -> AgentRunResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
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
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"command timed out after {timeout_seconds}s",
            timed_out=True,
            latency_ms=_latency_ms(started),
            backend_provider="claude_code",
        )
    except OSError as exc:
        return AgentRunResult(
            argv=list(argv),
            exit_code=None,
            error=str(exc),
            latency_ms=_latency_ms(started),
            backend_provider="claude_code",
        )
    return AgentRunResult(
        argv=list(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        session_id=_parse_session_id(completed.stdout),
        error=None
        if completed.returncode == 0
        else (completed.stderr.strip() or completed.stdout.strip()),
        latency_ms=_latency_ms(started),
        backend_provider="claude_code",
    )


@dataclass(frozen=True)
class _ParsedClaudeOutput:
    json_data: dict[str, Any] | None = None
    session_id: str | None = None
    error: str | None = None


def _parse_structured_output(stdout: str) -> _ParsedClaudeOutput:
    try:
        envelope = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        return _ParsedClaudeOutput(error=f"stdout was not valid JSON: {exc}")
    if not isinstance(envelope, dict):
        return _ParsedClaudeOutput(error="stdout JSON was not an object")
    session_id = _session_id_from_envelope(envelope)
    if envelope.get("is_error") is True:
        errors = envelope.get("errors")
        if isinstance(errors, list) and errors:
            return _ParsedClaudeOutput(
                session_id=session_id, error="; ".join(str(item) for item in errors)
            )
        return _ParsedClaudeOutput(
            session_id=session_id,
            error=str(envelope.get("subtype") or "Claude Code returned an error"),
        )
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return _ParsedClaudeOutput(json_data=structured, session_id=session_id)
    result = envelope.get("result")
    if isinstance(result, dict):
        return _ParsedClaudeOutput(json_data=result, session_id=session_id)
    if isinstance(result, str) and result.strip():
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            return _ParsedClaudeOutput(
                session_id=session_id,
                error=f"result was not valid JSON: {exc}",
            )
        if isinstance(parsed, dict):
            return _ParsedClaudeOutput(json_data=parsed, session_id=session_id)
        return _ParsedClaudeOutput(
            session_id=session_id,
            error="result JSON was not an object",
        )
    return _ParsedClaudeOutput(
        session_id=session_id,
        error="Claude Code did not produce structured output",
    )


def _parse_session_id(stdout: str) -> str | None:
    try:
        envelope = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    return _session_id_from_envelope(envelope)


def _session_id_from_envelope(envelope: dict[str, Any]) -> str | None:
    value = envelope.get("session_id")
    return value if isinstance(value, str) and value else None


def _with_claude_code_provider(
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
        backend_provider="claude_code",
    )


def _latency_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
