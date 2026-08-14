from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from .agent_backend import ReplyPostprocessBackend
from .agent_invocation import (
    AgentInvoker,
    agent_result_error,
    truncate_error,
)
from .config import AppConfig
from .paths import resolve_relative_path
from .prompt import ReplyPostprocessOutput, build_reply_postprocess_prompt
from .types import TaskRecord


@dataclass(frozen=True)
class ReplyPostprocessResult:
    applied: bool
    reply: str
    metadata: dict[str, Any]
    audit: dict[str, Any] | None = None
    failure_reason: str | None = None


class ReplyPostprocessor:
    def __init__(
        self,
        *,
        config: AppConfig,
        base_dir: str | Path,
        agent_backend: ReplyPostprocessBackend,
        agent_invoker: AgentInvoker,
    ):
        self.config = config
        self.base_dir = Path(base_dir)
        self.agent_backend = agent_backend
        self.agent_invoker = agent_invoker

    def run(
        self,
        *,
        task: TaskRecord,
        message_id: str,
        input_message_ids: list[str],
        original_reply: str,
        run_id: str,
        cwd: str | Path | None,
    ) -> ReplyPostprocessResult:
        cfg = self.config.reply_postprocess
        if not cfg.enabled:
            return ReplyPostprocessResult(
                applied=False, reply=original_reply, metadata={"applied": False}
            )
        if not original_reply.strip():
            return ReplyPostprocessResult(
                applied=False,
                reply=original_reply,
                metadata={
                    "applied": False,
                    "skipped": True,
                    "skip_reason": "empty_original_reply",
                },
            )
        guidance = self._guidance_paths()
        if guidance.error is not None:
            return self._failed(
                original_reply,
                failure_reason=guidance.error,
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
            )
        prompt = build_reply_postprocess_prompt(
            original_reply=original_reply,
            owner_style_profile_path=None
            if guidance.owner_style_resolved_path is None
            else str(guidance.owner_style_resolved_path),
            humanizer_skill_path=None
            if guidance.humanizer_resolved_path is None
            else str(guidance.humanizer_resolved_path),
        )
        outcome = self.agent_invoker.call_with_retries(
            lambda: self.agent_backend.reply_postprocess(prompt, cwd=cwd),
            run_id=run_id,
            stage="reply_postprocess",
            message_id=message_id,
            task_id=task.id,
        )
        result = outcome.result
        audit = {
            "result": result,
            "outcome": outcome,
            "prompt": prompt,
            "input_message_ids": input_message_ids,
        }
        if result is None or not result.ok or not isinstance(result.json_data, dict):
            failure = outcome.last_error or (
                None if result is None else agent_result_error(result)
            )
            return self._failed(
                original_reply,
                failure_reason="agent_failed",
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
                audit=audit,
                error=truncate_error(failure),
            )
        response_data: object = getattr(result, "json_data", None)
        try:
            output = ReplyPostprocessOutput.model_validate(
                cast(dict[str, Any], response_data)
            )
        except ValidationError as exc:
            return self._failed(
                original_reply,
                failure_reason="schema_failed",
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
                audit=audit,
                error=truncate_error(str(exc)),
            )
        if output.status == "needs_owner":
            return self._failed(
                original_reply,
                failure_reason="needs_owner",
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
                audit=audit,
            )
        final_reply = output.final_reply.strip()
        if not final_reply:
            return self._failed(
                original_reply,
                failure_reason="empty_final_reply",
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
                audit=audit,
            )
        if _length_guard_failed(original_reply=original_reply, final_reply=final_reply):
            return self._failed(
                original_reply,
                failure_reason="postprocess_length_growth",
                fallback="original_candidate",
                enabled_guidance=guidance.enabled_guidance,
                owner_style_profile_path=guidance.owner_style_configured_path,
                humanizer_skill_path=guidance.humanizer_configured_path,
                audit=audit,
            )
        metadata = {
            "applied": True,
            "status": "ok",
            "enabled_guidance": guidance.enabled_guidance,
            "original_reply": original_reply,
            "final_reply": final_reply,
        }
        if guidance.owner_style_configured_path is not None:
            metadata["owner_style_profile_path"] = guidance.owner_style_configured_path
        if guidance.humanizer_configured_path is not None:
            metadata["humanizer_skill_path"] = guidance.humanizer_configured_path
        return ReplyPostprocessResult(
            applied=True, reply=final_reply, metadata=metadata, audit=audit
        )

    def _guidance_paths(self) -> _GuidancePaths:
        cfg = self.config.reply_postprocess
        enabled: list[str] = []
        owner_configured: str | None = None
        owner_resolved: Path | None = None
        humanizer_configured: str | None = None
        humanizer_resolved: Path | None = None
        if cfg.owner_style.enabled:
            enabled.append("owner_style")
            owner_configured = cfg.owner_style.profile_path
            owner_resolved = resolve_relative_path(owner_configured, self.base_dir)
            if not _readable_file(owner_resolved):
                return _GuidancePaths(
                    enabled_guidance=enabled,
                    owner_style_configured_path=owner_configured,
                    owner_style_resolved_path=owner_resolved,
                    humanizer_configured_path=humanizer_configured,
                    humanizer_resolved_path=humanizer_resolved,
                    error="profile_missing",
                )
        if cfg.humanizer_zh.enabled:
            enabled.append("humanizer_zh")
            humanizer_configured = cfg.humanizer_zh.skill_path
            if humanizer_configured is None:
                return _GuidancePaths(
                    enabled_guidance=enabled,
                    owner_style_configured_path=owner_configured,
                    owner_style_resolved_path=owner_resolved,
                    humanizer_configured_path=None,
                    humanizer_resolved_path=None,
                    error="humanizer_skill_missing",
                )
            humanizer_resolved = resolve_relative_path(
                humanizer_configured, self.base_dir
            )
            if not _readable_file(humanizer_resolved):
                return _GuidancePaths(
                    enabled_guidance=enabled,
                    owner_style_configured_path=owner_configured,
                    owner_style_resolved_path=owner_resolved,
                    humanizer_configured_path=humanizer_configured,
                    humanizer_resolved_path=humanizer_resolved,
                    error="humanizer_missing",
                )
        return _GuidancePaths(
            enabled_guidance=enabled,
            owner_style_configured_path=owner_configured,
            owner_style_resolved_path=owner_resolved,
            humanizer_configured_path=humanizer_configured,
            humanizer_resolved_path=humanizer_resolved,
            error=None,
        )

    def _failed(
        self,
        original_reply: str,
        *,
        failure_reason: str,
        fallback: str,
        enabled_guidance: list[str],
        owner_style_profile_path: str | None,
        humanizer_skill_path: str | None,
        audit: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ReplyPostprocessResult:
        metadata: dict[str, Any] = {
            "applied": False,
            "status": "needs_owner" if failure_reason == "needs_owner" else "failed",
            "failure_reason": failure_reason,
            "fallback": fallback,
            "enabled_guidance": enabled_guidance,
        }
        if error is not None:
            metadata["error"] = error
        if owner_style_profile_path is not None:
            metadata["owner_style_profile_path"] = owner_style_profile_path
        if humanizer_skill_path is not None:
            metadata["humanizer_skill_path"] = humanizer_skill_path
        return ReplyPostprocessResult(
            applied=False,
            reply=original_reply,
            metadata=metadata,
            audit=audit,
            failure_reason=failure_reason,
        )


@dataclass(frozen=True)
class _GuidancePaths:
    enabled_guidance: list[str]
    owner_style_configured_path: str | None
    owner_style_resolved_path: Path | None
    humanizer_configured_path: str | None
    humanizer_resolved_path: Path | None
    error: str | None


def _readable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.R_OK)


def _length_guard_failed(*, original_reply: str, final_reply: str) -> bool:
    return (
        len(final_reply) > len(original_reply) * 3 and len(final_reply) > 300
    ) or len(final_reply) > 2000
