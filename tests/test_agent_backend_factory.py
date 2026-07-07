from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.agent_backend_factory import create_agent_backend
from feishu_shadow_agent.config import AppConfig, OwnerConfig
from feishu_shadow_agent.hermes import HermesCliClient


def test_backend_factory_builds_selected_hermes_backend_with_resolved_skills(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        agent_backend={
            "explicit_context": {"skills": ["skills/support/SKILL.md"]},
            "hermes": {"path": "/bin/hermes"},
        },
    )

    backend = create_agent_backend(config, base_dir=tmp_path)

    assert isinstance(backend, HermesCliClient)
    argv = backend.build_chat_command(
        prompt="prompt", max_turns=1, include_session_skills=True
    )

    assert backend.provider == "hermes"
    assert argv[argv.index("--toolsets") + 1] == "safe"
    assert argv[argv.index("--skills") + 1] == str(tmp_path / "skills" / "support")
