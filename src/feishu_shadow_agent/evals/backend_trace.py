from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from ..agent_backend import AgentBackend, AgentRunResult
from .artifacts import text_sha256
from .schemas import IngressJudgeOutput, SemanticJudgeOutput


class TracedAgentBackend:
    def __init__(self, backend: AgentBackend):
        self.backend = backend
        self.provider = backend.provider
        self._prompts: dict[str, list[str]] = {}
        self._task_session_ids: list[str] = []

    def task_router(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        self._record("router", prompt)
        return self.backend.task_router(prompt, cwd=cwd)

    def task_session(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        self._record("task_session", prompt)
        result = self.backend.task_session(prompt, session_id=session_id, cwd=cwd)
        if result.session_id and result.session_id not in self._task_session_ids:
            self._task_session_ids.append(result.session_id)
        return result

    def structured_output(
        self,
        prompt: str,
        *,
        output_model: type[BaseModel],
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> AgentRunResult:
        if output_model is IngressJudgeOutput:
            prompt_type = "ingress_judge"
        elif output_model is SemanticJudgeOutput:
            prompt_type = "semantic_judge"
        else:
            prompt_type = "structured_output"
        self._record(prompt_type, prompt)
        return self.backend.structured_output(
            prompt,
            output_model=output_model,
            session_id=session_id,
            cwd=cwd,
        )

    def reply_postprocess(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        self._record("reply_postprocess", prompt)
        return self.backend.reply_postprocess(prompt, cwd=cwd)

    def owner_style_refresh(
        self, prompt: str, *, cwd: str | Path | None = None
    ) -> AgentRunResult:
        self._record("owner_style_refresh", prompt)
        return self.backend.owner_style_refresh(prompt, cwd=cwd)

    def prompt_hashes(self) -> dict[str, str]:
        return {
            prompt_type: _ordered_prompt_hash(prompts)
            for prompt_type, prompts in self._prompts.items()
        }

    def task_session_ids(self) -> list[str]:
        return list(self._task_session_ids)

    def requested_skill_names(self) -> list[str]:
        getter = getattr(self.backend, "requested_skill_names", None)
        if not callable(getter):
            return []
        return list(getter())

    def write_prompts(self, directory: Path) -> None:
        if not self._prompts:
            return
        directory.mkdir(parents=True, exist_ok=True)
        for prompt_type, prompts in self._prompts.items():
            for index, prompt in enumerate(prompts, start=1):
                suffix = "" if len(prompts) == 1 else f"-{index:03d}"
                (directory / f"{prompt_type}{suffix}.txt").write_text(
                    prompt, encoding="utf-8"
                )

    def _record(self, prompt_type: str, prompt: str) -> None:
        self._prompts.setdefault(prompt_type, []).append(prompt)


def merge_prompt_hashes(rows: list[dict[str, str]]) -> dict[str, str]:
    prompt_types = sorted({key for row in rows for key in row})
    return {
        prompt_type: _merged_trial_hash(
            [row[prompt_type] for row in rows if prompt_type in row]
        )
        for prompt_type in prompt_types
    }


def _ordered_prompt_hash(prompts: list[str]) -> str:
    if len(prompts) == 1:
        return text_sha256(prompts[0])
    return _ordered_hashes_hash([text_sha256(prompt) for prompt in prompts])


def _ordered_hashes_hash(hashes: list[str]) -> str:
    if len(hashes) == 1:
        return hashes[0]
    return text_sha256(json.dumps(hashes, separators=(",", ":")))


def _merged_trial_hash(hashes: list[str]) -> str:
    if hashes and all(value == hashes[0] for value in hashes):
        return hashes[0]
    return _ordered_hashes_hash(hashes)
