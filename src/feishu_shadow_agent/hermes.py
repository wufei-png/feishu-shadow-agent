from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .agent_backend import AgentRunResult
from .config import AutoContextMode, ConfigScopeMode, HermesConfig, ToolPermissionsProfile

HermesRunner = Callable[[list[str], int], AgentRunResult]
SESSION_ID_RE = re.compile(r"session_id:\s*([^\s]+)")


@dataclass(frozen=True)
class HermesExecutionPolicy:
    toolsets: str
    yolo: bool = False

    def cli_args(self) -> list[str]:
        args = ["--toolsets", self.toolsets]
        if self.yolo:
            args.append("--yolo")
        return args


def hermes_execution_policy(profile: ToolPermissionsProfile) -> HermesExecutionPolicy:
    if profile == "read_only":
        return HermesExecutionPolicy(toolsets="safe")
    if profile == "guarded_write":
        return HermesExecutionPolicy(toolsets="hermes-cli")
    if profile == "full_access":
        return HermesExecutionPolicy(toolsets="hermes-cli", yolo=True)
    raise ValueError(f"unknown tool permissions profile: {profile}")


class HermesCliClient:
    provider = "hermes"

    def __init__(
        self,
        *,
        config: HermesConfig,
        tool_permissions: ToolPermissionsProfile = "guarded_write",
        config_scope: ConfigScopeMode = "isolated",
        auto_context: AutoContextMode = "disabled",
        session_skills: Sequence[str | Path] = (),
        cwd: str | Path | None = None,
        runner: HermesRunner | None = None,
    ):
        self.config = config
        self.execution_policy = hermes_execution_policy(tool_permissions)
        self.config_scope = config_scope
        self.auto_context = auto_context
        self.session_skills = [str(skill) for skill in session_skills]
        self.path = config.path or "hermes"
        self.cwd = None if cwd is None else Path(cwd)
        self._runner = runner

    def build_chat_command(
        self,
        *,
        prompt: str,
        max_turns: int,
        session_id: str | None = None,
        include_session_skills: bool = False,
    ) -> list[str]:
        argv = [
            self.path,
            "chat",
            "-q",
            prompt,
            "-Q",
            "--source",
            self.config.source,
            *self.execution_policy.cli_args(),
            "--max-turns",
            str(max_turns),
        ]
        if self.config_scope == "isolated":
            argv.append("--ignore-user-config")
        if self.auto_context == "disabled":
            argv.append("--ignore-rules")
        if include_session_skills:
            for skill in self.session_skills:
                argv.extend(["--skills", skill])
        if session_id:
            argv.extend(["--resume", session_id])
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.provider:
            argv.extend(["--provider", self.config.provider])
        return argv

    def task_router(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        return self._run(
            self.build_chat_command(prompt=prompt, max_turns=self.config.router_max_turns),
            cwd=cwd,
        )

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        return self._run(
            self.build_chat_command(
                prompt=prompt,
                max_turns=self.config.session_max_turns,
                session_id=session_id,
                include_session_skills=True,
            ),
            cwd=cwd,
        )

    def _run(self, argv: list[str], *, cwd: str | Path | None = None) -> AgentRunResult:
        run_cwd = self.cwd if cwd is None else Path(cwd)
        if self._runner is not None:
            result = self._runner(argv, self.config.timeout_seconds)
        else:
            result = _run_subprocess(argv, self.config.timeout_seconds, cwd=run_cwd)
        if not result.ok:
            return result
        # Hermes writes session metadata to stderr while stdout remains the strict
        # JSON contract consumed by the daemon.
        parsed_session_id = result.session_id or _parse_session_id(result.stderr)
        stdout = result.stdout.strip()
        try:
            json_data: Any = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return AgentRunResult(
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                session_id=parsed_session_id,
                error=f"stdout was not valid JSON: {exc}",
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


def _run_subprocess(argv: Sequence[str], timeout_seconds: int, *, cwd: Path | None = None) -> AgentRunResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
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
            backend_provider="hermes",
        )
    except OSError as exc:
        return AgentRunResult(
            argv=list(argv),
            exit_code=None,
            error=str(exc),
            latency_ms=_latency_ms(started),
            backend_provider="hermes",
        )
    if completed.returncode != 0:
        return AgentRunResult(
            argv=list(argv),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            session_id=_parse_session_id(completed.stderr),
            error=completed.stderr.strip() or completed.stdout.strip() or "command failed",
            latency_ms=_latency_ms(started),
            backend_provider="hermes",
        )
    return AgentRunResult(
        argv=list(argv),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        session_id=_parse_session_id(completed.stderr),
        latency_ms=_latency_ms(started),
        backend_provider="hermes",
    )


def _parse_session_id(stderr: str) -> str | None:
    match = SESSION_ID_RE.search(stderr or "")
    return match.group(1) if match else None


def _latency_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
