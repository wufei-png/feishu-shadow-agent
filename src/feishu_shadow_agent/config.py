from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

CONFIG_ENV_VAR = "FEISHU_SHADOW_AGENT_CONFIG"


class ConfigError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnerConfig(StrictModel):
    open_id: str = Field(min_length=1)
    name: str = ""


class DaemonConfig(StrictModel):
    tick_interval_seconds: int = Field(default=60, gt=0)
    overlap_seconds: int = Field(default=120, ge=0)


class HealthConfig(StrictModel):
    interval_seconds: int = Field(default=300, gt=0)
    retry_interval_seconds: int = Field(default=60, gt=0)
    timeout_seconds: int = Field(default=10, gt=0)


class StorageConfig(StrictModel):
    sqlite_path: str = "data/agent.sqlite3"
    resource_dir: str = "data/resources"

    @field_validator("resource_dir")
    @classmethod
    def validate_resource_dir(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("storage.resource_dir must be a safe relative path")
        return value


class LoggingConfig(StrictModel):
    jsonl_path: str = "logs/agent.jsonl"


class LarkCliConfig(StrictModel):
    path: str | None = None
    timeout_seconds: int = Field(default=30, gt=0)


class HermesConfig(StrictModel):
    mode: Literal["cli", "http"] = "cli"
    path: str | None = None
    source: str = "feishu-shadow-agent"
    router_max_turns: int = Field(default=4, gt=0)
    session_max_turns: int = Field(default=8, gt=0)
    model: str | None = None
    provider: str | None = None
    timeout_seconds: int = Field(default=60, gt=0)
    health_url: str | None = None
    api_key_env: str | None = "HERMES_API_KEY"

    @field_validator("health_url")
    @classmethod
    def validate_health_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError("hermes.health_url must start with http:// or https://")
        return value

    @field_validator("source")
    @classmethod
    def validate_non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value

    @model_validator(mode="after")
    def validate_http_mode(self) -> "HermesConfig":
        if self.mode == "http" and not self.health_url:
            raise ValueError("hermes.health_url is required when hermes.mode is http")
        return self


RiskLevel = Literal["low", "medium", "high"]
ToolPermissionsProfile = Literal["read_only", "guarded_write", "full_access"]


class ReplyPolicyConfig(StrictModel):
    p2p_auto_reply: StrictBool = True
    default_group_auto_reply: StrictBool = False
    risk_level_max: RiskLevel = "low"
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)


class ChatPolicyConfig(StrictModel):
    name: str = ""
    auto_reply: StrictBool = False
    bot_joined: StrictBool = False
    reply_identity: Literal["bot_preferred", "bot", "user"] = "bot_preferred"
    allow_user_fallback: StrictBool = True
    resource_download: StrictBool = True
    risk_level_max: RiskLevel = "low"
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)


class RetentionConfig(StrictModel):
    raw_message_days: int = Field(default=30, ge=1)
    resource_days: int = Field(default=30, ge=1)


class DebugConfig(StrictModel):
    save_full_hermes_io: StrictBool = False


class AppConfig(StrictModel):
    owner: OwnerConfig
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    lark_cli: LarkCliConfig = Field(default_factory=LarkCliConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    reply_policy: ReplyPolicyConfig = Field(default_factory=ReplyPolicyConfig)
    chats: dict[str, ChatPolicyConfig] = Field(default_factory=dict)
    tool_permissions: ToolPermissionsProfile = "guarded_write"
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)


@dataclass(frozen=True)
class LoadedConfig:
    config: AppConfig
    path: Path
    base_dir: Path


class ConfigService:
    def __init__(self, default_path: str | Path = "config.yaml", env_var: str = CONFIG_ENV_VAR):
        self.default_path = Path(default_path)
        self.env_var = env_var

    def resolve_path(self, explicit_path: str | Path | None = None) -> Path:
        if explicit_path:
            return Path(explicit_path).expanduser()
        env_path = os.environ.get(self.env_var)
        if env_path:
            return Path(env_path).expanduser()
        return self.default_path

    def load(self, explicit_path: str | Path | None = None) -> LoadedConfig:
        path = self.resolve_path(explicit_path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"failed to read config file: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        try:
            config = AppConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
        return LoadedConfig(config=config, path=path, base_dir=path.resolve().parent)

    def redacted_dict(self, config: AppConfig) -> dict[str, Any]:
        data = config.model_dump(mode="json")
        return self._redact_mapping(data)

    def _redact_mapping(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered.endswith("_env"):
                    # *_env values are environment variable names, not secrets.
                    # Keeping them visible makes redacted config output useful.
                    redacted[key] = self._redact_mapping(child)
                elif any(marker in lowered for marker in ("secret", "token", "api_key", "password")):
                    redacted[key] = "<redacted>"
                else:
                    redacted[key] = self._redact_mapping(child)
            return redacted
        if isinstance(value, list):
            return [self._redact_mapping(item) for item in value]
        return value
