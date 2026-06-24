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
    assert loaded.config.storage.resource_dir == "data/resources"
    assert loaded.config.logging.level == "info"
    assert loaded.config.logging.console is False
    assert loaded.config.logging.text_path == "logs/test.log"
    assert loaded.config.tool_permissions == "guarded_write"
    assert loaded.config.chats["oc_test"].auto_reply is True
    assert loaded.config.agent_backend.provider == "hermes"
    assert loaded.config.agent_backend.config_scope == "isolated"
    assert loaded.config.agent_backend.auto_context == "disabled"
    assert loaded.config.agent_backend.hermes.mode == "cli"


def test_tracked_config_schema_matches_generated_schema() -> None:
    tracked_schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    assert tracked_schema == ConfigService().json_schema_dict()


def test_missing_owner_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_permissions: guarded_write\n", encoding="utf-8")

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
  profile: guarded_write
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="tool_permissions"):
        ConfigService().load(config_path)


def test_top_level_hermes_config_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
hermes:
  mode: cli
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="hermes"):
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


def test_redacted_config_does_not_leak_env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_reserved_agent_backend_provider_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
agent_backend:
  provider: codex
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reserved but not implemented"):
        ConfigService().load(config_path)
