from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from feishu_shadow_agent.config import ConfigError, ConfigService

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.config.yaml"
SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schemas" / "config.schema.json"


def test_load_minimal_config() -> None:
    loaded = ConfigService().load(FIXTURE)

    assert loaded.config.owner.open_id == "ou_owner"
    assert loaded.reply_policy_used_defaults is False
    assert loaded.config.storage.resource_dir == "data/resources"
    assert loaded.config.storage.max_resource_bytes == 52_428_800
    assert loaded.config.storage.max_resource_dir_bytes == 2_147_483_648
    assert loaded.config.logging.level == "info"
    assert loaded.config.logging.console is False
    assert loaded.config.logging.text_path == "logs/test.log"
    assert loaded.config.tool_permissions == "read_only"
    assert loaded.config.chats["oc_test"].auto_reply is True
    assert loaded.config.reply_policy.unknown_group_auto_reply is False
    assert loaded.config.lifecycle.watch_minutes == 120
    assert loaded.config.lifecycle.burst_attach_seconds == 60
    assert loaded.config.lifecycle.closed_recall_days == 7
    assert loaded.config.lifecycle.approval_timeout_hours == 24
    assert loaded.config.retention.feedback_content_days == 30
    assert loaded.config.interactive_cards.enabled is False
    assert loaded.config.interactive_cards.app_id_env == "FEISHU_APP_ID"
    assert loaded.config.agent_backend.provider == "hermes"
    assert loaded.config.agent_backend.max_attempts == 3
    assert loaded.config.agent_backend.working_dir is None
    assert loaded.config.agent_backend.config_scope == "isolated"
    assert loaded.config.agent_backend.auto_context == "disabled"
    assert loaded.config.agent_backend.hermes.mode == "cli"
    assert loaded.config.agent_backend.codex.path is None
    assert loaded.config.agent_backend.codex.model is None
    assert loaded.config.agent_backend.codex.reasoning_effort is None
    assert loaded.config.agent_backend.claude_code.path is None
    assert loaded.config.agent_backend.claude_code.model is None
    assert loaded.config.reply_postprocess.enabled is False
    assert loaded.config.reply_postprocess.max_turns == 4
    assert loaded.config.reply_postprocess.owner_style.enabled is False
    assert loaded.config.reply_postprocess.humanizer_zh.enabled is False


def test_logging_jsonl_and_text_paths_must_resolve_to_different_files(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
logging:
  jsonl_path: logs/agent.log
  text_path: ./logs/agent.log
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must resolve to different files"):
        ConfigService().load(config_path)


def test_agent_backend_timeouts_default_to_unlimited(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("owner:\n  open_id: ou_owner\n", encoding="utf-8")

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.hermes.timeout_seconds is None
    assert loaded.config.agent_backend.codex.timeout_seconds is None
    assert loaded.config.agent_backend.claude_code.timeout_seconds is None


@pytest.mark.parametrize("tool_permissions", ["read_only", "full_access"])
def test_agent_max_attempts_is_shared_by_tool_permission_profiles(
    tmp_path: Path, tool_permissions: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
owner:
  open_id: ou_owner
agent_backend:
  max_attempts: 5
tool_permissions: {tool_permissions}
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.max_attempts == 5


def test_agent_max_attempts_must_be_positive(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "owner:\n  open_id: ou_owner\nagent_backend:\n  max_attempts: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_attempts"):
        ConfigService().load(config_path)


@pytest.mark.parametrize(
    "cards_yaml",
    [
        "app_id_env: literal-secret-value!",
        "app_id_env: SAME\n  app_secret_env: SAME",
    ],
)
def test_interactive_card_credentials_must_be_distinct_environment_names(
    tmp_path: Path, cards_yaml: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"owner:\n  open_id: ou_owner\ninteractive_cards:\n  {cards_yaml}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="interactive card"):
        ConfigService().load(config_path)


def test_tracked_config_schema_matches_generated_schema() -> None:
    tracked_schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    assert tracked_schema == ConfigService().json_schema_dict()


def test_feedback_metadata_deletion_setting_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
retention:
  feedback_metadata_days: 365
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="feedback_metadata_days"):
        ConfigService().load(config_path)


def test_agent_backend_provider_schema_accepts_supported_backends() -> None:
    schema = ConfigService().json_schema_dict()
    provider_schema = schema["$defs"]["AgentBackendConfig"]["properties"]["provider"]

    assert provider_schema.get("enum") == ["hermes", "codex", "claude_code"]
    assert "const" not in provider_schema


def test_missing_owner_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_permissions: read_only\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="owner"):
        ConfigService().load(config_path)


def test_invalid_tool_permission_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
tool_permissions: dangerous
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="tool_permissions"):
        ConfigService().load(config_path)


def test_nested_tool_permissions_config_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
tool_permissions:
  profile: read_only
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="tool_permissions"):
        ConfigService().load(config_path)


def test_guarded_write_tool_permission_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
tool_permissions: guarded_write
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="tool_permissions"):
        ConfigService().load(config_path)


def test_legacy_top_level_hermes_config_is_migrated(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
hermes:
  mode: cli
  path: /bin/hermes
debug:
  save_full_hermes_io: true
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.provider == "hermes"
    assert loaded.config.agent_backend.hermes.path == "/bin/hermes"
    assert loaded.config.debug.save_full_agent_io is True


def test_legacy_top_level_hermes_config_merges_with_partial_agent_backend(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
hermes:
  path: /bin/hermes
agent_backend:
  config_scope: native
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.config_scope == "native"
    assert loaded.config.agent_backend.hermes.path == "/bin/hermes"


def test_ambiguous_legacy_and_new_hermes_config_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
hermes:
  mode: cli
agent_backend:
  provider: hermes
  hermes:
    mode: cli
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"agent_backend\.hermes"):
        ConfigService().load(config_path)


def test_ambiguous_legacy_and_new_debug_agent_io_config_is_rejected(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
debug:
  save_full_agent_io: false
  save_full_hermes_io: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="save_full_hermes_io"):
        ConfigService().load(config_path)


def test_chat_policy_requires_strict_bool(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
chats:
  oc_test:
    auto_reply: "yes"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="auto_reply"):
        ConfigService().load(config_path)


def test_legacy_default_group_auto_reply_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_policy:
  default_group_auto_reply: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="default_group_auto_reply"):
        ConfigService().load(config_path)


def test_loaded_config_reports_reply_policy_defaults_for_import_source(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.reply_policy_used_defaults is True
    assert loaded.config.reply_policy.p2p_auto_reply is True
    assert loaded.config.reply_policy.unknown_group_auto_reply is False


def test_approval_timeout_can_be_null(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
lifecycle:
  approval_timeout_hours: null
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.lifecycle.approval_timeout_hours is None


def test_resource_dir_must_be_safe_relative_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
storage:
  resource_dir: ../outside
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="resource_dir"):
        ConfigService().load(config_path)


def test_resource_quota_values_must_be_positive(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
storage:
  max_resource_bytes: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_resource_bytes"):
        ConfigService().load(config_path)


def test_reply_postprocess_enabled_requires_guidance_source(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_postprocess:
  enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match=r"owner_style\.enabled or humanizer_zh\.enabled"
    ):
        ConfigService().load(config_path)


def test_disabled_humanizer_allows_null_skill_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_postprocess:
  humanizer_zh:
    enabled: false
    skill_path: null
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.reply_postprocess.humanizer_zh.skill_path is None


def test_enabled_humanizer_requires_skill_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
reply_postprocess:
  enabled: true
  humanizer_zh:
    enabled: true
    skill_path: null
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"humanizer_zh\.enabled requires skill_path"):
        ConfigService().load(config_path)


def test_reply_postprocess_model_provider_can_inherit_main_hermes_settings(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  hermes:
    model: main-model
    provider: main-provider
reply_postprocess:
  enabled: true
  model: null
  provider: null
  owner_style:
    enabled: true
    profile_path: data/owner_style.zh.md
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.reply_postprocess.model is None
    assert loaded.config.reply_postprocess.provider is None
    assert loaded.config.agent_backend.hermes.model == "main-model"
    assert loaded.config.agent_backend.hermes.provider == "main-provider"


def test_redacted_config_does_not_leak_env_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_API_KEY", "super-secret-value")
    service = ConfigService()
    loaded = service.load(FIXTURE)

    redacted = service.redacted_dict(loaded.config)

    assert redacted["agent_backend"]["hermes"]["api_key_env"] == "HERMES_API_KEY"
    assert "super-secret-value" not in str(redacted)
    assert os.environ["HERMES_API_KEY"] == "super-secret-value"


def test_http_hermes_mode_requires_health_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: hermes
  hermes:
    mode: http
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="health_url"):
        ConfigService().load(config_path)


def test_agent_backend_working_dir_strips_whitespace(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  working_dir: " ./agent-root "
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.working_dir == "./agent-root"


def test_agent_backend_working_dir_rejects_empty_string(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  working_dir: "  "
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="working_dir"):
        ConfigService().load(config_path)


def test_codex_agent_backend_provider_is_accepted(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
  codex:
    path: /bin/codex
    model: gpt-5.6-luna
    reasoning_effort: xhigh
    skills: [docmate, review-agent]
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.provider == "codex"
    assert loaded.config.agent_backend.codex.path == "/bin/codex"
    assert loaded.config.agent_backend.codex.model == "gpt-5.6-luna"
    assert loaded.config.agent_backend.codex.reasoning_effort == "xhigh"
    assert loaded.config.agent_backend.codex.skills == ["docmate", "review-agent"]


@pytest.mark.parametrize("name", ["DocMate", "bad_name", "-bad", "bad-"])
def test_codex_skill_names_reject_non_native_syntax(tmp_path: Path, name: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
owner:
  open_id: ou_owner
agent_backend:
  codex:
    skills: [{name}]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"codex\.skills"):
        ConfigService().load(config_path)


def test_explicit_context_paths_must_be_absolute_and_prompt_safe(
    tmp_path: Path,
) -> None:
    for value in ["relative/skill", "/tmp/`skill`", "/tmp/skill\\nname"]:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"""
owner:
  open_id: ou_owner
agent_backend:
  explicit_context:
    paths: ["{value}"]
""",
            encoding="utf-8",
        )

        with pytest.raises(ConfigError, match=r"explicit_context\.paths"):
            ConfigService().load(config_path)


def test_claude_code_agent_backend_provider_is_accepted(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: claude_code
  claude_code:
    path: /bin/claude
    model: sonnet
""",
        encoding="utf-8",
    )

    loaded = ConfigService().load(config_path)

    assert loaded.config.agent_backend.provider == "claude_code"
    assert loaded.config.agent_backend.claude_code.path == "/bin/claude"
    assert loaded.config.agent_backend.claude_code.model == "sonnet"


def test_unsupported_agent_backend_provider_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: openhands
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        ConfigService().load(config_path)

    error = str(exc_info.value)
    assert "agent_backend" in error
    assert "provider" in error
    assert "hermes" in error
    assert "codex" in error
    assert "claude_code" in error
