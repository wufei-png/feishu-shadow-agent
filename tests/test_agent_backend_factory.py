from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.agent_backend_factory import create_agent_backend
from feishu_shadow_agent.claude_code import ClaudeCodeCliClient
from feishu_shadow_agent.codex import CodexCliClient
from feishu_shadow_agent.config import AgentBackendConfig, AppConfig, OwnerConfig
from feishu_shadow_agent.hermes import HermesCliClient


def test_backend_factory_builds_selected_hermes_backend_with_resolved_skills(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        agent_backend=AgentBackendConfig.model_validate(
            {
                "hermes": {
                    "path": "/bin/hermes",
                    "skill_paths": ["skills/support/SKILL.md"],
                },
            }
        ),
    )

    backend = create_agent_backend(config, base_dir=tmp_path)

    assert isinstance(backend, HermesCliClient)
    argv = backend.build_chat_command(
        prompt="prompt", max_turns=1, include_session_skills=True
    )

    assert backend.provider == "hermes"
    assert argv[argv.index("--toolsets") + 1] == "safe"
    assert argv[argv.index("--skills") + 1] == str(tmp_path / "skills" / "support")


def test_backend_factory_builds_selected_codex_backend() -> None:
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        agent_backend=AgentBackendConfig.model_validate(
            {"provider": "codex", "codex": {"path": "/bin/codex"}}
        ),
    )

    backend = create_agent_backend(config, base_dir=Path("/tmp"))

    assert isinstance(backend, CodexCliClient)
    argv = backend.build_exec_command(
        output_schema_path="/tmp/schema.json",
        output_path="/tmp/output.json",
    )

    assert backend.provider == "codex"
    assert argv[0] == "/bin/codex"
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_backend_factory_configures_native_names_and_explicit_paths_for_codex(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "skills" / "support"
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        agent_backend=AgentBackendConfig.model_validate(
            {
                "provider": "codex",
                "explicit_context": {"paths": [str(context_path)]},
                "codex": {"path": "/bin/codex", "skills": ["docmate"]},
            }
        ),
    )

    backend = create_agent_backend(config, base_dir=tmp_path)

    assert isinstance(backend, CodexCliClient)
    assert backend.session_skill_names == ["docmate"]
    assert backend.explicit_context_paths == [str(context_path)]


def test_backend_factory_builds_selected_claude_code_backend() -> None:
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner"),
        agent_backend=AgentBackendConfig.model_validate(
            {
                "provider": "claude_code",
                "claude_code": {"path": "/bin/claude"},
            }
        ),
    )

    backend = create_agent_backend(config, base_dir=Path("/tmp"))

    assert isinstance(backend, ClaudeCodeCliClient)
    argv = backend.build_print_command(output_schema={"type": "object"})

    assert backend.provider == "claude_code"
    assert argv[0] == "/bin/claude"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
