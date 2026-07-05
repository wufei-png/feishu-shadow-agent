from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

AgentBackendProvider = Literal["hermes", "codex", "claude_code"]


@dataclass(frozen=True)
class AgentRunResult:
    argv: list[str]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    json_data: Any | None = None
    session_id: str | None = None
    error: str | None = None
    timed_out: bool = False
    latency_ms: int | None = None
    backend_provider: AgentBackendProvider | str = "hermes"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None and not self.timed_out


class AgentBackend(Protocol):
    provider: AgentBackendProvider | str

    def task_router(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        ...

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        ...

    def reply_postprocess(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        ...

    def owner_style_refresh(self, prompt: str, *, cwd: str | Path | None = None) -> AgentRunResult:
        ...
