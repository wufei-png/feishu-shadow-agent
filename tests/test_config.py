from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishu_shadow_agent.config import ConfigError, ConfigService


FIXTURE = Path(__file__).parent / "fixtures" / "minimal.config.yaml"


def test_load_minimal_config() -> None:
    loaded = ConfigService().load(FIXTURE)

    assert loaded.config.owner.open_id == "ou_owner"
    assert loaded.config.storage.resource_dir == "data/resources"
    assert loaded.config.tool_permissions.profile == "guarded_write"
    assert loaded.config.chats["oc_test"].auto_reply is True
    assert loaded.config.hermes.mode == "cli"
    assert loaded.config.hermes.toolsets == "safe"


def test_missing_owner_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_permissions:\n  profile: guarded_write\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="owner"):
        ConfigService().load(config_path)


def test_invalid_tool_permission_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
tool_permissions:
  profile: dangerous
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="profile"):
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

    assert redacted["hermes"]["api_key_env"] == "HERMES_API_KEY"
    assert "super-secret-value" not in str(redacted)
    assert os.environ["HERMES_API_KEY"] == "super-secret-value"


def test_http_hermes_mode_requires_health_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
owner:
  open_id: ou_owner
hermes:
  mode: http
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="health_url"):
        ConfigService().load(config_path)
