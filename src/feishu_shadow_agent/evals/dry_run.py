from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..agent_backend import AgentRunResult
from .schemas import IngressJudgeOutput, SemanticJudgeOutput


class DryRunBackend:
    provider = "dry_run"

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._result(
            "task_router",
            {
                "route": "ambiguous",
                "target_task_id": None,
                "reason": "dry_run_backend",
            },
        )

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        data: dict[str, Any] = {
            "answerability": "no_reply",
            "proposed_reply": "",
            "reply_target_message_id": None,
            "watch_action": "keep_watching",
        }
        if session_id is None:
            data["task_label"] = "dry-run"
        return self._result(
            "task_session", data, session_id=session_id or "dry-run-session"
        )

    def structured_output(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        if output_model is SemanticJudgeOutput:
            data: dict[str, Any] = {"verdict": "pass", "differences": []}
        elif output_model is IngressJudgeOutput:
            data = {"labels": []}
        else:
            return AgentRunResult(
                argv=["dry-run-backend", "structured_output"],
                exit_code=1,
                error=f"unsupported dry-run output model: {output_model.__name__}",
                backend_provider=self.provider,
            )
        return self._result("structured_output", data, session_id=session_id)

    def reply_postprocess(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._result("reply_postprocess", {"status": "ok", "final_reply": ""})

    def owner_style_refresh(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        return self._result(
            "owner_style_refresh", {"status": "failed", "profile_markdown": ""}
        )

    def _result(
        self,
        request: str,
        data: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            argv=["dry-run-backend", request],
            exit_code=0,
            json_data=data,
            session_id=session_id,
            backend_provider=self.provider,
        )
