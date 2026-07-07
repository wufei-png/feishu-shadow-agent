from __future__ import annotations

from pathlib import Path

from .agent_backend import AgentBackend
from .codex import CodexCliClient
from .config import AppConfig
from .hermes import HermesCliClient
from .paths import resolve_agent_skill_path


def create_agent_backend(config: AppConfig, *, base_dir: str | Path) -> AgentBackend:
    backend = config.agent_backend
    if backend.provider == "hermes":
        session_skills = [
            resolve_agent_skill_path(skill, base_dir)
            for skill in backend.explicit_context.skills
        ]
        return HermesCliClient(
            config=backend.hermes,
            tool_permissions=config.tool_permissions,
            config_scope=backend.config_scope,
            auto_context=backend.auto_context,
            reply_postprocess=config.reply_postprocess,
            session_skills=session_skills,
        )
    if backend.provider == "codex":
        return CodexCliClient(
            config=backend.codex,
            tool_permissions=config.tool_permissions,
            config_scope=backend.config_scope,
            auto_context=backend.auto_context,
            reply_postprocess=config.reply_postprocess,
        )
    raise ValueError(f"unsupported agent backend provider: {backend.provider}")
